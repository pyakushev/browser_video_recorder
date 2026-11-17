"""zoom_worker.py - background worker that renders zoom sidecars into videos.

It runs as a separate container in ``docker compose`` (same image as ``web``),
claims ``zoom_jobs`` rows with ``FOR UPDATE SKIP LOCKED`` and calls
``apply_zoom.py`` with ffmpeg.  The processed file is stored next to the
original upload; its URL is written to ``videos.processed_video_url``.

Run locally::

    python zoom_worker.py --once
    python zoom_worker.py --poll 1

Configuration (environment variables):
    DB_*                  PostgreSQL connection (same as web_service.py)
    PUBLIC_BASE_URL       public base for the processed URL (default http://localhost:8000)
    FFMPEG_BIN            ffmpeg binary (default: ffmpeg)
    WORKER_POLL_SEC       idle polling interval (default 1.0)
    ZOOM_TIMEOUT_SEC      ffmpeg timeout per video (default 1800)
    ZOOM_MAX_ATTEMPTS     attempts before a job fails (default 3)
    WORKER_STALE_SEC      running jobs older than this are re-queued (default 1800)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from web_service import db_connection, init_db, close_pool
from zoom_sidecar import sidecar_path

logger = logging.getLogger("zoom_worker")

CLAIM_SQL = """
WITH next AS (
    SELECT id
    FROM zoom_jobs
    WHERE status = 'pending'
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE zoom_jobs AS j
SET status = 'running', started_at = NOW(), attempts = j.attempts + 1
FROM next
WHERE j.id = next.id
RETURNING j.id, j.video_id, j.attempts
"""

STALE_SQL = """
WITH stale AS (
    UPDATE zoom_jobs
    SET status = 'pending', started_at = NULL
    WHERE status = 'running'
      AND (started_at IS NULL OR started_at < NOW() - (%s * INTERVAL '1 second'))
    RETURNING video_id
)
UPDATE videos AS v
SET zoom_status = 'pending', updated_at = NOW()
FROM stale
WHERE v.id = stale.video_id
"""


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    ffmpeg: str
    poll_sec: float
    timeout_sec: float
    max_attempts: int
    stale_sec: float


def settings_from_env(args: Optional[argparse.Namespace] = None) -> Settings:
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    return Settings(
        public_base_url=base.rstrip("/"),
        ffmpeg=(getattr(args, "ffmpeg", None) or os.environ.get("FFMPEG_BIN") or "ffmpeg"),
        poll_sec=float(getattr(args, "poll", None) or os.environ.get("WORKER_POLL_SEC", "1")),
        timeout_sec=float(
            getattr(args, "timeout", None) or os.environ.get("ZOOM_TIMEOUT_SEC", "1800")
        ),
        max_attempts=int(getattr(args, "max_attempts", None) or os.environ.get("ZOOM_MAX_ATTEMPTS", "3")),
        stale_sec=float(os.environ.get("WORKER_STALE_SEC", "1800")),
    )


def claim_job() -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest pending job and load its video row."""
    with db_connection() as conn, conn.cursor() as cur:
        while True:
            cur.execute(CLAIM_SQL)
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT id, company_name, file_path, zoom_sidecar_path, processed_file_path "
                "FROM videos WHERE id = %s",
                (row["video_id"],),
            )
            video = cur.fetchone()
            if video is not None:
                break
            # FK makes this impossible, but fail the job and claim the next one.
            cur.execute(
                "UPDATE zoom_jobs SET status = 'failed', error = 'video row disappeared', "
                "finished_at = NOW() WHERE id = %s",
                (row["id"],),
            )
        cur.execute(
            "UPDATE videos SET zoom_status = 'running', zoom_error = NULL, "
            "updated_at = NOW() WHERE id = %s",
            (video["id"],),
        )
        return {
            "id": int(row["id"]),
            "video_id": int(row["video_id"]),
            "attempts": int(row["attempts"]),
            "video": dict(video),
        }


def _finish_job(
    job: Dict[str, Any],
    status: str,
    error: Optional[str] = None,
    output: Optional[Path] = None,
    public_base_url: str = "http://localhost:8000",
) -> bool:
    """Persist the job result.  Returns False if the job was superseded."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM zoom_jobs WHERE id = %s FOR UPDATE", (job["id"],)
        )
        current = cur.fetchone()
        if current is None or current["status"] != "running":
            return False

        cur.execute(
            "UPDATE zoom_jobs SET status = %s, error = %s, finished_at = NOW() WHERE id = %s",
            (status, error, job["id"]),
        )
        video_id = job["video_id"]
        if status == "done" and output is not None:
            url = f"{public_base_url}/media/{quote(output.name)}"
            cur.execute(
                "UPDATE videos SET processed_file_path = %s, processed_video_url = %s, "
                "zoom_status = 'done', zoom_error = NULL, processed_at = NOW(), "
                "updated_at = NOW() WHERE id = %s",
                (str(output), url, video_id),
            )
        elif status == "skipped":
            cur.execute(
                "UPDATE videos SET zoom_status = 'skipped', zoom_error = %s, "
                "updated_at = NOW() WHERE id = %s",
                (error, video_id),
            )
        else:
            cur.execute(
                "UPDATE videos SET zoom_status = 'failed', zoom_error = %s, "
                "updated_at = NOW() WHERE id = %s",
                (error, video_id),
            )
    return True


def process_job(job: Dict[str, Any], settings: Settings) -> str:
    """Run one claimed job; returns its final status."""
    video = job["video"]
    video_id = job["video_id"]
    video_path = Path(video["file_path"])
    sidecar = video["zoom_sidecar_path"] or str(sidecar_path(video_path))

    if job["attempts"] > settings.max_attempts:
        detail = f"too many attempts ({job['attempts']} > {settings.max_attempts})"
        logger.error("job %s: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"
    if not video_path.is_file():
        detail = f"video file is missing: {video_path}"
        logger.error("job %s: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"
    if not Path(sidecar).is_file():
        detail = f"no zoom sidecar for {video_path.name}"
        logger.warning("job %s: %s", job["id"], detail)
        _finish_job(job, "skipped", detail, public_base_url=settings.public_base_url)
        return "skipped"

    output = video_path.with_name(f"{video_path.stem}_zoomed.webm")
    apply_script = Path(__file__).resolve().with_name("apply_zoom.py")
    command = [
        sys.executable,
        str(apply_script),
        "--video",
        str(video_path),
        "--sidecar",
        str(sidecar),
        "--out",
        str(output),
        "--ffmpeg",
        settings.ffmpeg,
    ]
    logger.info("job %s: %s -> %s", job["id"], video_path.name, output.name)

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.timeout_sec,
        )
    except subprocess.TimeoutExpired:
        detail = f"apply_zoom timed out after {settings.timeout_sec:.0f} s"
        logger.error("job %s: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"
    except FileNotFoundError:
        detail = f"python/ffmpeg not found ({settings.ffmpeg!r})"
        logger.error("job %s: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"
    except OSError as exc:
        detail = f"cannot start apply_zoom: {exc}"
        logger.error("job %s: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "apply_zoom failed").strip()[-4000:]
        logger.error("job %s: apply_zoom failed: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"

    if not output.is_file() or output.stat().st_size == 0:
        detail = "apply_zoom produced no output"
        logger.error("job %s: %s", job["id"], detail)
        _finish_job(job, "failed", detail, public_base_url=settings.public_base_url)
        return "failed"

    if not _finish_job(
        job, "done", output=output, public_base_url=settings.public_base_url
    ):
        # The upload was replaced while ffmpeg was running: drop this result.
        logger.warning("job %s was superseded; removing %s", job["id"], output)
        output.unlink(missing_ok=True)
        return "cancelled"
    logger.info("job %s done: %s", job["id"], output.name)
    return "done"


def reset_stale_jobs(stale_sec: float) -> None:
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(STALE_SQL, (int(max(1, stale_sec)),))


def run_once(settings: Settings) -> Tuple[int, int]:
    processed = 0
    failed = 0
    while True:
        job = claim_job()
        if job is None:
            return processed, failed
        status = process_job(job, settings)
        processed += 1
        if status == "failed":
            failed += 1


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process queued zoom jobs with ffmpeg.")
    parser.add_argument("--once", action="store_true", help="process pending jobs and exit")
    parser.add_argument("--poll", type=float, default=None, help="idle polling interval, seconds")
    parser.add_argument("--timeout", type=float, default=None, help="ffmpeg timeout, seconds")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg binary")
    parser.add_argument("--max-attempts", type=int, default=None, help="attempts per job")
    return parser.parse_args(argv)


_stop = False


def _handle_stop(_signum: int, _frame: Any) -> None:
    global _stop
    _stop = True


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = settings_from_env(args)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    init_db()
    reset_stale_jobs(settings.stale_sec)
    logger.info(
        "worker started: ffmpeg=%s, poll=%.1fs, timeout=%.0fs, max_attempts=%d",
        settings.ffmpeg,
        settings.poll_sec,
        settings.timeout_sec,
        settings.max_attempts,
    )

    try:
        if args.once:
            processed, failed = run_once(settings)
            logger.info("processed %d job(s), %d failed", processed, failed)
            return 1 if failed else 0

        while not _stop:
            job = claim_job()
            if job is None:
                time.sleep(max(0.05, settings.poll_sec))
                continue
            process_job(job, settings)
    finally:
        close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
