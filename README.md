# Browser video recorder + zoom post-processing + video web service (Docker)

Three parts that work together:

1. **Recorder** (`capture_video.py`, `qscript.py`): for every row of a CSV file it records a
   `.webm` video of an automated browser session (Playwright + Chromium) and can capture
   Camtasia-style zoom triggers (`F5` or `window.__zoomAt(x, y)`). When at least one zoom was
   triggered, it writes a JSON sidecar next to the video: `<video>.webm.zoom.json`.
2. **Web service** (`web_service.py`, FastAPI + PostgreSQL) running in Docker Compose: it stores
   uploaded videos + sidecars, queues zoom post-processing, looks videos up by company name and
   serves both the original and the processed video.
3. **Zoom worker** (`zoom_worker.py`, same image, ffmpeg): claims queued jobs and renders the zoom
   with `apply_zoom.py`; the processed URL is stored in PostgreSQL.


## Quick start (service)

```bash
cp env.example .env              # set DB_PASSWORD (the file is git-ignored)
docker compose up --build        # web on http://localhost:8000, API docs on /docs
```

The image installs `ffmpeg`/`ffprobe`, so the worker can render zooms inside Docker. The first
build is slow because it downloads the Debian packages and Python dependencies; later builds use
the cache.

```bash
# original upload (still works exactly as before; no zoom)
curl -F "file=@video/ACME GmbH.webm" -F "company_name=ACME GmbH" \
     http://localhost:8000/upload

# upload + sidecar; the web service queues the zoom automatically
curl -F "file=@video/ACME GmbH.webm" \
     -F "zoom_sidecar=@video/ACME GmbH.webm.zoom.json" \
     -F "company_name=ACME GmbH" \
     http://localhost:8000/upload

curl "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
curl -i "http://localhost:8000/get_video_url?company_name=unknown"      # 404
curl http://localhost:8000/health
curl http://localhost:8000/videos
```

Data survives restarts: `docker compose down` followed by `docker compose up` keeps the database
and the uploaded videos (named volumes `db_data` and `uploads`). `docker compose down -v` deletes
the volumes, i.e. all data.

Useful commands:

```bash
docker compose logs -f web                     # application logs
docker compose logs -f worker                  # zoom worker logs
docker compose ps                              # health status of the services
docker compose exec db psql -U video_user -d videos \
  -c "select id, company_name, zoom_status, processed_video_url from videos;"
docker compose exec worker ffmpeg -version     # ffmpeg is installed in the image
```

If you change `WEB_PORT`, also set `PUBLIC_BASE_URL` to the address users actually open; the worker
uses it to build the `processed_video_url` stored in PostgreSQL.

## Services

| Service | Image | Notes |
|---|---|---|
| `web` | built from `Dockerfile` (`python:3.12-slim`) | uvicorn with `WEB_CONCURRENCY=2` workers, runs as a non-root user, port `8000:8000`, volume `uploads:/app/uploads` |
| `worker` | the same image (`playwright-zoom-service:local`) | `python -m zoom_worker`, no published port, volume `uploads:/app/uploads`, `init: true`, `stop_grace_period: 60s` |
| `db` | `postgres:16` | volume `db_data:/var/lib/postgresql/data`, **no published ports** (reachable only from `web`/`worker`) |

`docker compose up --build` builds the image through the `web` service; `worker` reuses the same
`playwright-zoom-service:local` tag, so ffmpeg is installed in exactly one image.

| Variable | Default | Meaning |
|---|---|---|
| `DB_HOST`, `DB_PORT` | `db`/`5432` in compose | PostgreSQL address |
| `DB_NAME`, `DB_USER` | `videos`, `video_user` | database and user |
| `DB_PASSWORD` | **required** | password (from `.env`); `DB_PASSWORD_FILE` reads it from a file (Docker secrets) |
| `DB_POOL_MAX` | `10` | max connections per worker process |
| `DB_CONNECT_RETRIES`, `DB_CONNECT_DELAY` | `30`, `1` s | waiting for the database |
| `UPLOAD_DIR` | `./uploads` (`/app/uploads` in the image) | where videos are stored |
| `MAX_UPLOAD_MB` | `500` | upload size limit |
| `MAX_SIDECAR_KB` | `2048` | zoom sidecar size limit |
| `WEB_PORT` | `8000` | host port published by compose |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | public base URL used by the worker for `processed_video_url` |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg binary used by the worker |
| `WORKER_POLL_SEC` | `1` | idle polling interval |
| `ZOOM_TIMEOUT_SEC` | `1800` | ffmpeg timeout per video |
| `ZOOM_MAX_ATTEMPTS` | `3` | attempts before a job is marked failed |
| `WORKER_STALE_SEC` | `1800` | running jobs older than this are re-queued on worker start |

## Zoom capture (recorder)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m playwright install chromium

python capture_video.py                                  # input.csv -> ./video
python capture_video.py --headed                         # show the recording browser
python capture_video.py --no-auto-zoom                   # only F5 / __zoomAt trigger a zoom
python capture_video.py --zoom-factor 2.5 --zoom-ramp 0.4
python capture_video.py --server http://localhost:8000 --wait-processed
```

- **F5** toggles the zoom at the current mouse position: the first press starts the zoom-in, the
  second one starts the zoom-out. The overlay calls `preventDefault()` so Chromium does not reload
  the page.
- **`window.__zoomAt(x, y)`** is the programmatic hook; the sample scenario uses it for two demo
  zooms (around the search box and around the first result). The automation never presses F5.
- A colored ring (cyan for `in`, red for `out`) and a `ZOOM 2.0×` badge give the operator visual
  feedback.
- Events are stored as `{"t": seconds, "x": px, "y": px, "type": "in" | "out"}` relative to the
  start of the recording (the moment the video page is created).
- The sidecar is written **only when at least one event exists**; a stale sidecar from an older
  recording is removed otherwise. A failed sidecar write never discards the video.

Example sidecar:

```json
{
  "viewport": {"width": 1280, "height": 720},
  "fps": 25,
  "zoom_factor": 2.0,
  "ramp_sec": 0.5,
  "events": [
    {"t": 3.0, "x": 640, "y": 360, "type": "in"},
    {"t": 8.0, "x": 640, "y": 360, "type": "out"}
  ]
}
```

`--server` uploads the video and, if present, the sidecar. `--wait-processed` polls the job and
previews the processed video when the worker is done (falling back to the original on failure).
A failed row, upload or preview is logged and the batch continues.

Input CSV: UTF-8, `;` delimiter, `"` qualifier, header
`URL;Company Name;Language;Country;Postcode;City;Street;HouseNo`.

## Zoom post-processing

```bash
python apply_zoom.py --video video/ACME_GmbH.webm
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run
python apply_zoom.py --video video/ACME_GmbH.webm --out video/ACME_GmbH_zoomed.webm
python apply_zoom.py --video video/ACME_GmbH.webm --ffmpeg /usr/bin/ffmpeg
```

- Reads `--sidecar` (default `<video>.zoom.json`), pairs each `in` with the next `out`, and renders
  `zoompan`: ease-in ramp → hold → ease-out ramp.
- The crop is `iw/zoom × ih/zoom`, centred on the mouse point and clamped to the frame:
  `x = clamp(px - iw/(2*zoom), 0, iw - iw/zoom)` (same for `y`). Viewport pixels are mapped 1:1 to
  video pixels when the sizes match; otherwise they are scaled.
- Multiple non-overlapping in/out pairs are supported. An `in` without a following `out` holds the
  zoom to the end of the video. If the hold is shorter than two ramps, the ramps shrink so they
  never overlap.
- Output is a new `.webm` (`libvpx-vp9`, `-crf 32`) with the input resolution preserved. The file is
  written atomically via a temporary file, so a reader never sees a partial video.
- No sidecar or no events → the video is copied unchanged (`shutil.copy2`).
- `--dry-run` prints the full ffmpeg command (shell-quoted) and does not encode.
- `--ffmpeg` points at another binary (or use `FFMPEG_BIN`); `ffprobe` is taken from the same
  directory or from `PATH`.
- `--fps`, `--zoom-factor` and `--ramp-sec` override the sidecar values; `--crf`/`--cpu-used` tune
  VP9 encoding (`--cpu-used` 0–8: higher is faster and lower quality).

Legacy sidecars without `type` are accepted: events alternate `in`/`out` in order, starting with
`in`.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | multipart `file` + `company_name` + optional `zoom_sidecar`; stores the file(s), upserts the record and queues the zoom when a sidecar is present |
| GET | `/get_video_url?company_name=...` | returns `company_name`, `video_url`, `processed_video_url`, `zoom_status`, `zoom_error`; `404` if unknown |
| GET | `/media/<filename>` | serves an uploaded or processed video (plays in a browser) |
| GET | `/videos` | lists all records, including zoom fields |
| GET | `/videos/{id}` | one record with its zoom status |
| POST | `/videos/{id}/process` | queue (or re-queue) processing of an uploaded video; `409` if it has no sidecar |
| GET | `/jobs/{id}` | job status (`pending/running/done/failed/skipped/cancelled`), error and processed URL |
| POST | `/jobs/{id}/process` | manual retry of a finished/failed job; `409` if it is still active |
| GET | `/health` | liveness + database check |

### Database

PostgreSQL table `videos(id, company_name UNIQUE, file_path, video_url, zoom_sidecar_path,
processed_file_path, processed_video_url, zoom_status, zoom_error, processed_at, created_at,
updated_at)`. Only filesystem paths and URLs are stored, never video bytes. Uploading the same
`company_name` again updates the existing row (`INSERT ... ON CONFLICT (company_name) DO UPDATE`,
no duplicates), cancels an older pending/running job and clears the old processed URL.

`zoom_jobs(id, video_id, status, attempts, error, created_at, started_at, finished_at)` tracks the
worker queue. A partial unique index prevents two active jobs for the same video. The worker claims
work with `FOR UPDATE SKIP LOCKED`, so several workers can share the queue safely.

### Status lifecycle

```
none ──(sidecar upload)──► pending ──► running ──► done
                                   └────────────► failed
                                   └────────────► skipped   (sidecar file disappeared)
                                   └────────────► cancelled (re-upload superseded it)
```

The processed file is stored next to the original as `<name>_zoomed.webm`; its URL is written to
`videos.processed_video_url` and served by `GET /media/<filename>`.

## Tests

```bash
pip install -r requirements-dev.txt

pytest -m "not integration and not docker"   # unit tests only
pytest -m integration                         # ffmpeg + Playwright capture
RUN_DOCKER_TESTS=1 pytest -m docker           # full Docker stack (builds an isolated project)
```

- Unit tests cover the zoom-filter generation, coordinate mapping/clamping, ramp frame math,
  multiple pairs, open-ended zooms, sidecar parsing, legacy sidecars without `type`, and the
  "no events → copy unchanged" path.
- The ffmpeg integration test generates a `testsrc` clip, applies the zoom and checks the output
  resolution/duration; it is skipped if ffmpeg is not installed.
- The Playwright test checks `window.__zoomAt`, F5, the on-screen ring and sidecar writing; it is
  skipped if the Chromium browser is not installed.
- The Docker test builds an isolated project on port `18080`, uploads a video + sidecar, waits for
  the worker, checks the processed URL (HTTP 200), the PostgreSQL row and that the worker image has
  `ffmpeg`. It is opt-in and tears down its volumes afterwards.

## Design notes

- **FastAPI/uvicorn instead of Apache/mod_wsgi.** The web service from Internship Task 1 is an ASGI
  app; uvicorn keeps lifespan events, streaming uploads and `FileResponse` working. Apache with a
  reverse proxy can still be put in front of `web` later.
- **Zoom is a post effect**, not a live zoom: Playwright records a fixed viewport and the worker
  renders the effect with ffmpeg. `record_video_size == viewport`, so mouse pixels map 1:1.
- **The worker is a separate container** so ffmpeg never blocks HTTP requests and the queue survives
  a restart. It uses the same image and the same `uploads` volume as `web`.
- **Uploaded videos are served by the application** (`/media/...`); for a larger deployment they
  would be served by a proxy or object storage.
- The database password is not in git: `.env` is ignored, `.env.example` holds a placeholder, and
  compose refuses to start without `DB_PASSWORD`. For production use Docker secrets
  (`DB_PASSWORD_FILE` is supported by the application).
- The processed output is written through a temporary file + atomic rename; a failed ffmpeg run
  keeps the original video untouched.
- DuckDuckGo may answer automated recorder sessions with HTTP 418 or a captcha depending on the
  network; the recorder then returns `None` for that row and continues.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `ffmpeg not found` in the worker | rebuild the image (`docker compose build web`) and check `docker compose exec worker ffmpeg -version`; or set `FFMPEG_BIN` |
| `zoom_status = failed` | `GET /jobs/{id}` (`zoom_error`) or `docker compose logs worker`; the original video is intact |
| `zoom_status = skipped` | the job found no sidecar file; upload `zoom_sidecar` or run `apply_zoom.py` locally (a video uploaded without a sidecar gets `none`, not `skipped`) |
| processed URL points to the wrong host | set `PUBLIC_BASE_URL` to the public address (and keep `WEB_PORT` in sync) |
| `apply_zoom.py --dry-run` says "no zoom events" | `<video>.zoom.json` is missing or its `events` list is empty; a copy is intentional |
| VP9 encoding is slow | raise `--cpu-used` (0–8) or `--crf` to trade quality for speed, or run more workers |
