"""web_service.py - video upload / lookup service (FastAPI + PostgreSQL).

Run locally:
    python web_service.py          # listens on 0.0.0.0:8000
    FLASK_HOST=127.0.0.1 FLASK_PORT=9000 python web_service.py
Run in Docker:
    docker compose up --build      # see README.md

Endpoints:
    POST /upload                    multipart: file + company_name  (UploadVideo)
    GET  /get_video_url             ?company_name=...               (GetVideoURL)
    GET  /media/<filename>          serves an uploaded video
    GET  /videos                    lists all records (optional)
    GET  /health                    liveness + database check
Interactive API docs: http://<host>:<port>/docs

Configuration (environment variables, nothing is hard-coded):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD   PostgreSQL connection
    DB_PASSWORD_FILE        read the password from a file (Docker secrets)
    DB_POOL_MAX             max connections per process (default 10)
    DB_CONNECT_RETRIES      attempts while waiting for the database (default 30)
    DB_CONNECT_DELAY        seconds between attempts (default 1)
    UPLOAD_DIR              where videos are stored (default ./uploads)
    MAX_UPLOAD_MB           upload size limit (default 500)
    FLASK_HOST, FLASK_PORT  only for `python web_service.py`

Storage: videos live in UPLOAD_DIR, metadata (company name, absolute file path,
public URL) in the PostgreSQL table `videos`. Video bytes are never stored in the
database.
"""

import hashlib
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional
from urllib.parse import quote

import psycopg2
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("uvicorn.error")  # -> `docker compose logs web`

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads"))).resolve()
TMP_DIR = UPLOAD_DIR / ".tmp"  # partial uploads, same volume (atomic rename); never served

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
CHUNK_SIZE = 1024 * 1024

POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
CONNECT_RETRIES = int(os.environ.get("DB_CONNECT_RETRIES", "30"))
CONNECT_DELAY = float(os.environ.get("DB_CONNECT_DELAY", "1"))

MEDIA_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,                       -- absolute path to the file on disk
    video_url TEXT NOT NULL,                       -- public URL to the video
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


UPSERT_SQL = """
INSERT INTO videos (company_name, file_path, video_url)
VALUES (%s, %s, %s)
ON CONFLICT (company_name) DO UPDATE SET
    file_path = EXCLUDED.file_path,
    video_url = EXCLUDED.video_url,
    updated_at = NOW()
"""

SCHEMA_LOCK_ID = 727_001  # arbitrary constant for pg_advisory_xact_lock


def db_settings() -> Dict[str, Any]:
    password = os.environ.get("DB_PASSWORD")
    password_file = os.environ.get("DB_PASSWORD_FILE")
    if password_file:  # Docker secrets
        password = Path(password_file).read_text(encoding="utf-8").strip()
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ.get("DB_NAME", "videos"),
        "user": os.environ.get("DB_USER", "video_user"),
        "password": password,
    }


_pool: Optional[ThreadedConnectionPool] = None
_pool_slots: Optional[threading.BoundedSemaphore] = None
_pool_lock = threading.Lock()


def _open_pool() -> ThreadedConnectionPool:
    """Create the pool, waiting for the database to accept connections."""
    settings = db_settings()
    last_error: Optional[Exception] = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            return ThreadedConnectionPool(
                1, POOL_MAX, cursor_factory=RealDictCursor, connect_timeout=5, **settings
            )
        except psycopg2.OperationalError as exc:
            last_error = exc
            logger.warning(
                "database is not ready (attempt %d/%d): %s", attempt, CONNECT_RETRIES, str(exc).strip()
            )
            time.sleep(CONNECT_DELAY)
    raise RuntimeError(
        f"database at {settings['host']}:{settings['port']} is unreachable "
        f"after {CONNECT_RETRIES} attempts"
    ) from last_error


def get_pool() -> ThreadedConnectionPool:
    global _pool, _pool_slots
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool_slots = threading.BoundedSemaphore(POOL_MAX)
                _pool = _open_pool()
    return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def db_connection() -> Iterator[Any]:
    """Borrow a connection; commit on success, roll back on any error.

    psycopg2's pool raises PoolError when it is exhausted instead of waiting,
    so a semaphore of the same size makes extra requests wait for a free
    connection. Connections are never shared between threads at the same time.
    """
    pool = get_pool()
    assert _pool_slots is not None
    with _pool_slots:
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except psycopg2.Error:
                pass  # the connection is broken; putconn() discards it
            raise
        finally:
            pool.putconn(conn)


def init_db() -> None:
    """Create the upload folders and the `videos` table (idempotent)."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn, conn.cursor() as cur:
        # Several workers start at once: CREATE TABLE IF NOT EXISTS can race, so serialize it.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
        cur.execute(SCHEMA)


def safe_stem(company_name: str) -> str:
    """'ACME GmbH' -> 'ACME_GmbH'. Keeps letters (incl. non-ASCII), digits, . _ -"""
    stem = re.sub(r"[^\w.\-]+", "_", company_name.strip())
    stem = stem.strip("._-")[:120]
    return stem or "video"


def choose_filename(cur: Any, company_name: str, ext: str) -> str:
    """File name for a company; a short hash is added only on a clash.

    'A/B' and 'A_B' both sanitize to 'A_B', and they must not overwrite each
    other's video. The same company always gets the same name back, so a
    re-upload replaces its own file.
    """
    stem = safe_stem(company_name)
    candidate = stem + ext
    cur.execute(
        "SELECT company_name FROM videos WHERE lower(file_path) = lower(%s)",
        (str(UPLOAD_DIR / candidate),),
    )
    owner = cur.fetchone()
    if owner is not None and owner["company_name"] != company_name:
        digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]
        candidate = f"{stem}_{digest}{ext}"
    return candidate


def save_upload_to_tmp(upload: UploadFile) -> Path:
    """Stream the upload to a temp file (never the whole video in memory)."""
    tmp_path = TMP_DIR / f"{uuid.uuid4().hex}.part"
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = upload.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                    )
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="uploaded file is empty")
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()  # waits for the database (retries), then creates the table
    yield
    close_pool()


app = FastAPI(title="Video upload service", version="2.0.0", lifespan=lifespan)


@app.post("/upload")
def upload_video(
    request: Request,
    file: UploadFile = File(...),
    company_name: str = Form(...),
) -> Dict[str, str]:
    """UploadVideo: store the file, upsert the record, return the video URL."""
    company = company_name.strip()
    if not company:
        raise HTTPException(status_code=400, detail="company_name must not be empty")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail="unsupported file type %r (allowed: %s)" % (ext, ", ".join(sorted(MEDIA_TYPES))),
        )

    tmp_path = save_upload_to_tmp(file)
    base_url = str(request.base_url)  # e.g. 'http://localhost:8000/' - never hard-coded
    previous_path = None
    try:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (safe_stem(company).lower(),)
            )
            cur.execute("SELECT file_path FROM videos WHERE company_name = %s", (company,))
            previous = cur.fetchone()
            if previous is not None:
                previous_path = previous["file_path"]

            filename = choose_filename(cur, company, ext)
            final_path = UPLOAD_DIR / filename
            os.replace(str(tmp_path), str(final_path))
            video_url = f"{base_url}media/{quote(filename)}"
            cur.execute(UPSERT_SQL, (company, str(final_path), video_url))
    finally:
        tmp_path.unlink(missing_ok=True)
    
    if previous_path and previous_path != str(final_path):
        try:
            Path(previous_path).unlink()
        except OSError:
            pass

    return {"company_name": company, "video_url": video_url}


@app.get("/get_video_url")
def get_video_url(company_name: str = Query(...)) -> Dict[str, str]:
    """GetVideoURL: look the video up by company name (404 if unknown)."""
    company = company_name.strip()
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT company_name, video_url FROM videos WHERE company_name = %s", (company,)
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no video for company {company!r}")
    return {"company_name": row["company_name"], "video_url": row["video_url"]}


@app.api_route("/media/{filename}", methods=["GET", "HEAD"])
def get_media(filename: str) -> FileResponse:
    """Serve an uploaded video so that the returned URL plays in a browser."""
    if Path(filename).name != filename or filename.startswith("."):
        raise HTTPException(status_code=404, detail="not found")
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type = MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(filename)[0]
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/videos")
def list_videos() -> List[Dict[str, Any]]:
    """(optional) list all records."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, company_name, file_path, video_url, created_at, updated_at "
            "FROM videos ORDER BY id"
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe that also checks the database (used by the compose healthcheck)."""
    try:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


def main() -> None:
    import uvicorn  # imported here so that importing this module does not need it

    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
