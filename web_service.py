"""web_service.py - video upload / lookup service (FastAPI + SQLite).

Run:
    python web_service.py          # listens on 0.0.0.0:8000
    FLASK_HOST=127.0.0.1 FLASK_PORT=9000 python web_service.py

Endpoints:
    POST /upload                    multipart: file + company_name  (UploadVideo)
    GET  /get_video_url             ?company_name=...               (GetVideoURL)
    GET  /media/<filename>          serves an uploaded video
    GET  /videos                    lists all records (optional)
Interactive API docs: http://<host>:<port>/docs

Storage: videos live in ./uploads next to this file, metadata (company name,
absolute file path, public URL) in ./videos.db (SQLite). Video bytes are never
stored in the database.

Optional env var: MAX_UPLOAD_MB (default 500) - upload size limit.
"""

import hashlib
import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
TMP_DIR = UPLOAD_DIR / ".tmp"  # partial uploads; never served
DB_PATH = BASE_DIR / "videos.db"

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
CHUNK_SIZE = 1024 * 1024

MEDIA_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,      -- absolute path to the file on disk
    video_url TEXT NOT NULL,      -- public URL to the video
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

UPSERT_SQL = """
INSERT INTO videos (company_name, file_path, video_url)
VALUES (?, ?, ?)
ON CONFLICT(company_name) DO UPDATE SET
    file_path = excluded.file_path,
    video_url = excluded.video_url,
    updated_at = datetime('now')
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    """A new connection per request, opened and closed in the same thread.

    sync endpoints run in a thread pool, and SQLite connections must not be
    shared between threads, so every endpoint opens its own.
    """
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create ./uploads and videos.db (WAL mode) on first start."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def safe_stem(company_name: str) -> str:
    """'ACME GmbH' -> 'ACME_GmbH'. Keeps letters (incl. non-ASCII), digits, . _ -"""
    stem = re.sub(r"[^\w.\-]+", "_", company_name.strip())
    stem = stem.strip("._-")[:120]
    return stem or "video"


def choose_filename(conn: sqlite3.Connection, company_name: str, ext: str) -> str:
    """File name for a company; a short hash is added only on a clash.

    'A/B' and 'A_B' both sanitize to 'A_B', and they must not overwrite each
    other's video. The same company always gets the same name back, so a
    re-upload replaces its own file.
    """
    stem = safe_stem(company_name)
    candidate = stem + ext
    owner = conn.execute(
        "SELECT company_name FROM videos WHERE lower(file_path) = lower(?)",
        (str(UPLOAD_DIR / candidate),),
    ).fetchone()
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
    init_db()
    yield


app = FastAPI(title="Video upload service", version="1.0.0", lifespan=lifespan)


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
    base_url = str(request.base_url)
    previous_path = None
    try:
        with db_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                previous = conn.execute(
                    "SELECT file_path FROM videos WHERE company_name = ?", (company,)
                ).fetchone()
                if previous is not None:
                    previous_path = previous["file_path"]

                filename = choose_filename(conn, company, ext)
                final_path = UPLOAD_DIR / filename
                os.replace(str(tmp_path), str(final_path))
                video_url = f"{base_url}media/{quote(filename)}"
                conn.execute(UPSERT_SQL, (company, str(final_path), video_url))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
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
    with db_connection() as conn:
        row = conn.execute(
            "SELECT company_name, video_url FROM videos WHERE company_name = ?", (company,)
        ).fetchone()
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
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, company_name, file_path, video_url, created_at, updated_at "
            "FROM videos ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def main() -> None:
    import uvicorn  # imported here so that importing this module does not need it

    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
