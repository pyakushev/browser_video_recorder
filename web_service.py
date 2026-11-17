"""web_service.py - video upload / lookup service (FastAPI + PostgreSQL).

Run locally:
    python web_service.py          # listens on 0.0.0.0:8000
    FLASK_HOST=127.0.0.1 FLASK_PORT=9000 python web_service.py
Run in Docker:
    docker compose up --build      # see README.md

Endpoints:
    POST /upload                    multipart: file + company_name + optional zoom_sidecar
    GET  /get_video_url             ?company_name=...               (GetVideoURL)
    GET  /media/<filename>          serves an uploaded or processed video
    GET  /videos                    lists all records
    GET  /videos/{id}               one record with its zoom status
    POST /videos/{id}/process       queue/re-queue zoom post-processing
    GET  /jobs/{id}                 zoom job status + processed URL
    POST /jobs/{id}/process         manual retry of a finished/failed job
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
    MAX_SIDECAR_KB          zoom sidecar size limit (default 2048)
    FLASK_HOST, FLASK_PORT  only for `python web_service.py`

Storage: videos and sidecars live in UPLOAD_DIR, metadata (company name, absolute
paths, public URLs, zoom status) in the PostgreSQL tables `videos` and `zoom_jobs`.
Video bytes are never stored in the database.

Zoom processing is performed by `zoom_worker.py` (a separate container using this
same image); the worker writes `videos.processed_video_url`. See README.md.
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

from zoom_sidecar import SidecarError, ZoomSidecar, loads_sidecar, sidecar_path, write_sidecar

logger = logging.getLogger("uvicorn.error")  # -> `docker compose logs web`

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads"))).resolve()
TMP_DIR = UPLOAD_DIR / ".tmp"  # partial uploads, same volume (atomic rename); never served

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
MAX_SIDECAR_BYTES = int(os.environ.get("MAX_SIDECAR_KB", "2048")) * 1024
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
    zoom_sidecar_path TEXT,                        -- absolute path to <video>.zoom.json
    processed_file_path TEXT,                      -- absolute path to the zoomed file
    processed_video_url TEXT,                      -- public URL to the zoomed file
    zoom_status TEXT NOT NULL DEFAULT 'none',      -- none|pending|running|done|failed|skipped
    zoom_error TEXT,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

#: Applied to databases created by the previous version of the service.
MIGRATIONS = """
ALTER TABLE videos ADD COLUMN IF NOT EXISTS zoom_sidecar_path TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_file_path TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_video_url TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS zoom_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS zoom_error TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
"""

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS zoom_jobs (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS zoom_jobs_status_idx ON zoom_jobs (status);
CREATE UNIQUE INDEX IF NOT EXISTS zoom_jobs_active_video_idx
    ON zoom_jobs (video_id) WHERE status IN ('pending', 'running');
"""


UPSERT_SQL = """
INSERT INTO videos (company_name, file_path, video_url, zoom_sidecar_path, zoom_status)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (company_name) DO UPDATE SET
    file_path = EXCLUDED.file_path,
    video_url = EXCLUDED.video_url,
    zoom_sidecar_path = EXCLUDED.zoom_sidecar_path,
    processed_file_path = NULL,
    processed_video_url = NULL,
    processed_at = NULL,
    zoom_error = NULL,
    zoom_status = EXCLUDED.zoom_status,
    updated_at = NOW()
RETURNING id
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
        cur.execute(MIGRATIONS)
        cur.execute(JOBS_SCHEMA)


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


def read_sidecar_upload(upload: Optional[UploadFile]) -> Optional[ZoomSidecar]:
    """Validate the optional sidecar part; return None when there is nothing to do."""
    if upload is None:
        return None
    raw = upload.file.read(MAX_SIDECAR_BYTES + 1)
    if len(raw) > MAX_SIDECAR_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"zoom sidecar is larger than {MAX_SIDECAR_BYTES // 1024} KB",
        )
    if not raw.strip():
        return None
    try:
        sidecar = loads_sidecar(raw)
    except SidecarError as exc:
        raise HTTPException(status_code=400, detail=f"invalid zoom sidecar: {exc}") from exc
    return sidecar if sidecar.has_events else None


def cancel_active_jobs(cur: Any, video_id: int) -> None:
    """A re-upload supersedes any pending/running job for the same video."""
    cur.execute(
        "UPDATE zoom_jobs SET status = 'cancelled', finished_at = NOW(), "
        "error = COALESCE(error, 'superseded by a new upload') "
        "WHERE video_id = %s AND status IN ('pending', 'running')",
        (video_id,),
    )


def enqueue_job(cur: Any, video_id: int) -> Optional[int]:
    """Insert a pending job unless one is already active for this video."""
    cur.execute(
        "INSERT INTO zoom_jobs (video_id) "
        "SELECT %s WHERE NOT EXISTS ("
        "    SELECT 1 FROM zoom_jobs "
        "    WHERE video_id = %s AND status IN ('pending', 'running')"
        ") RETURNING id",
        (video_id, video_id),
    )
    row = cur.fetchone()
    return int(row["id"]) if row is not None else None


def active_job_id(cur: Any, video_id: int) -> Optional[int]:
    cur.execute(
        "SELECT id FROM zoom_jobs WHERE video_id = %s "
        "AND status IN ('pending', 'running') ORDER BY id LIMIT 1",
        (video_id,),
    )
    row = cur.fetchone()
    return int(row["id"]) if row is not None else None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()  # waits for the database (retries), then creates the table
    yield
    close_pool()


app = FastAPI(title="Video upload service", version="2.1.0", lifespan=lifespan)


@app.post("/upload")
def upload_video(
    request: Request,
    file: UploadFile = File(...),
    company_name: str = Form(...),
    zoom_sidecar: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    """UploadVideo: store the file + optional zoom sidecar, queue post-processing."""
    company = company_name.strip()
    if not company:
        raise HTTPException(status_code=400, detail="company_name must not be empty")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail="unsupported file type %r (allowed: %s)" % (ext, ", ".join(sorted(MEDIA_TYPES))),
        )

    sidecar = read_sidecar_upload(zoom_sidecar)
    tmp_path = save_upload_to_tmp(file)
    base_url = str(request.base_url)  # e.g. 'http://localhost:8000/' - never hard-coded

    previous_path: Optional[str] = None
    previous_processed: Optional[str] = None
    previous_sidecar: Optional[str] = None
    final_path: Optional[Path] = None
    sidecar_file: Optional[Path] = None
    job_id: Optional[int] = None
    video_url = ""
    try:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (safe_stem(company).lower(),)
            )
            cur.execute(
                "SELECT file_path, processed_file_path, zoom_sidecar_path "
                "FROM videos WHERE company_name = %s",
                (company,),
            )
            previous = cur.fetchone()
            if previous is not None:
                previous_path = previous["file_path"]
                previous_processed = previous["processed_file_path"]
                previous_sidecar = previous["zoom_sidecar_path"]

            filename = choose_filename(cur, company, ext)
            final_path = UPLOAD_DIR / filename
            sidecar_file = sidecar_path(final_path)
            os.replace(str(tmp_path), str(final_path))
            if sidecar is not None:
                write_sidecar(sidecar_file, sidecar)
            else:
                sidecar_file.unlink(missing_ok=True)

            video_url = f"{base_url}media/{quote(filename)}"
            zoom_status = "pending" if sidecar is not None else "none"
            cur.execute(
                UPSERT_SQL,
                (
                    company,
                    str(final_path),
                    video_url,
                    str(sidecar_file) if sidecar is not None else None,
                    zoom_status,
                ),
            )
            row = cur.fetchone()
            video_id = int(row["id"])

            cancel_active_jobs(cur, video_id)  # a re-upload supersedes old work
            if sidecar is not None:
                job_id = enqueue_job(cur, video_id)
                if job_id is None:  # extremely unlikely, but keep the status truthful
                    job_id = active_job_id(cur, video_id)
                if job_id is None:
                    cur.execute(
                        "UPDATE videos SET zoom_status = 'none' WHERE id = %s", (video_id,)
                    )
    finally:
        tmp_path.unlink(missing_ok=True)

    # Old artifacts are removed only after the transaction committed.
    if previous_path and final_path is not None and previous_path != str(final_path):
        try:
            Path(previous_path).unlink()
        except OSError:
            pass
    if previous_processed:
        try:
            Path(previous_processed).unlink()
        except OSError:
            pass
    if previous_sidecar and (sidecar_file is None or previous_sidecar != str(sidecar_file)):
        try:
            Path(previous_sidecar).unlink()
        except OSError:
            pass

    return {
        "company_name": company,
        "video_url": video_url,
        "processed_video_url": None,
        "zoom_status": "pending" if job_id is not None else "none",
        "job_id": job_id,
    }


@app.get("/get_video_url")
def get_video_url(company_name: str = Query(...)) -> Dict[str, Any]:
    """GetVideoURL: look the video up by company name (404 if unknown)."""
    company = company_name.strip()
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT company_name, video_url, processed_video_url, zoom_status, zoom_error "
            "FROM videos WHERE company_name = %s",
            (company,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no video for company {company!r}")
    return {
        "company_name": row["company_name"],
        "video_url": row["video_url"],
        "processed_video_url": row["processed_video_url"],
        "zoom_status": row["zoom_status"],
        "zoom_error": row["zoom_error"],
    }


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
    """(optional) list all records, including processing status."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, company_name, file_path, video_url, zoom_sidecar_path, "
            "processed_file_path, processed_video_url, zoom_status, zoom_error, "
            "processed_at, created_at, updated_at "
            "FROM videos ORDER BY id"
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@app.get("/videos/{video_id}")
def get_video(video_id: int) -> Dict[str, Any]:
    """One video record with the zoom status and the processed URL."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, company_name, file_path, video_url, zoom_sidecar_path, "
            "processed_file_path, processed_video_url, zoom_status, zoom_error, "
            "processed_at, created_at, updated_at "
            "FROM videos WHERE id = %s",
            (video_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no video with id {video_id}")
    return dict(row)


@app.post("/videos/{video_id}/process", status_code=202)
def process_video(video_id: int) -> Dict[str, Any]:
    """Queue (or re-queue) the zoom post-processing of an uploaded video."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, file_path, zoom_sidecar_path FROM videos WHERE id = %s",
            (video_id,),
        )
        video = cur.fetchone()
        if video is None:
            raise HTTPException(status_code=404, detail=f"no video with id {video_id}")
        sidecar = video["zoom_sidecar_path"]
        if not sidecar or not Path(sidecar).is_file():
            raise HTTPException(
                status_code=409,
                detail="this video has no zoom sidecar; upload one or apply the zoom locally",
            )

        existing = active_job_id(cur, video_id)
        if existing is None:
            job_id = enqueue_job(cur, video_id)
            status = "pending" if job_id is not None else "none"
            if job_id is not None:
                cur.execute(
                    "UPDATE videos SET zoom_status = 'pending', zoom_error = NULL, "
                    "updated_at = NOW() WHERE id = %s",
                    (video_id,),
                )
        else:
            job_id = existing
            cur.execute("SELECT status FROM zoom_jobs WHERE id = %s", (job_id,))
            status = cur.fetchone()["status"]
    return {"video_id": video_id, "job_id": job_id, "status": status}


@app.get("/jobs/{job_id}")
def get_job(job_id: int) -> Dict[str, Any]:
    """Processing job status + the resulting URL."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.video_id, j.status, j.attempts, j.error, "
            "j.created_at, j.started_at, j.finished_at, "
            "v.company_name, v.video_url, v.processed_video_url, v.zoom_status "
            "FROM zoom_jobs j JOIN videos v ON v.id = j.video_id "
            "WHERE j.id = %s",
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
    return dict(row)


@app.post("/jobs/{job_id}/process", status_code=202)
def process_job(job_id: int) -> Dict[str, Any]:
    """Re-queue a finished/failed job (manual retry)."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, video_id, status FROM zoom_jobs WHERE id = %s FOR UPDATE",
            (job_id,),
        )
        job = cur.fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
        if job["status"] in ("pending", "running"):
            raise HTTPException(status_code=409, detail="job is already active")

        cancel_active_jobs(cur, job["video_id"])
        cur.execute(
            "UPDATE zoom_jobs SET status = 'pending', attempts = 0, error = NULL, "
            "started_at = NULL, finished_at = NULL WHERE id = %s",
            (job_id,),
        )
        cur.execute(
            "UPDATE videos SET zoom_status = 'pending', zoom_error = NULL, "
            "updated_at = NOW() WHERE id = %s",
            (job["video_id"],),
        )
    return {"job_id": job_id, "video_id": job["video_id"], "status": "pending"}


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
