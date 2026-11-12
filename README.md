# Browser video recorder + video web service (Docker)

Two parts that work together:

1. **Recorder** (`capture_video.py`, `qscript.py`): for every row of a CSV file it records a
   `.webm` video of an automated browser session (Playwright + Chromium).
2. **Web service** (`web_service.py`, FastAPI + PostgreSQL) running in Docker Compose: it
   stores uploaded videos, looks them up by company name and serves them for preview.
   `video_client.py` is the client used by the recorder.

```
host:8000 ──► web (uvicorn + FastAPI) ──► db (postgres:16)
                   │                            │
             volume: uploads              volume: db_data
```

## Quick start (service)

```bash
cp .env.example .env              # set DB_PASSWORD (the file is git-ignored)
docker compose up --build         # web on http://localhost:8000, API docs on /docs
```

```bash
curl -F "file=@video/ACME GmbH.webm" -F "company_name=ACME GmbH" http://localhost:8000/upload
# {"company_name":"ACME GmbH","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}

curl "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
curl -i "http://localhost:8000/get_video_url?company_name=unknown"      # 404
curl http://localhost:8000/health
```

Data survives restarts: `docker compose down` followed by `docker compose up` keeps the database
and the uploaded videos (named volumes `db_data` and `uploads`). `docker compose down -v`
deletes the volumes, i.e. all data.

Useful commands:

```bash
docker compose logs -f web                     # application logs
docker compose ps                              # health status of both services
docker compose exec db psql -U video_user -d videos -c "select id, company_name, file_path, video_url from videos;"
```

## Services

| Service | Image | Notes |
|---|---|---|
| `web` | built from `Dockerfile` (`python:3.12-slim`) | uvicorn with `WEB_CONCURRENCY=2` workers, runs as a non-root user, port `8000:8000`, volume `uploads:/app/uploads` |
| `db` | `postgres:16` | volume `db_data:/var/lib/postgresql/data`, **no published ports** (reachable only from `web`) |

- `web` starts only after `db` is healthy (`depends_on: condition: service_healthy`, `pg_isready`).
  In addition the application retries the connection on startup (`DB_CONNECT_RETRIES`), so it
  does not crash if the database restarts or is slow; if it still gives up, `restart:
  unless-stopped` starts it again.
- `web` has its own healthcheck: `GET /health` also checks the database connection.
- Configuration is passed via environment variables, nothing is hard-coded:

| Variable | Default | Meaning |
|---|---|---|
| `DB_HOST`, `DB_PORT` | `db`/`5432` in compose | PostgreSQL address |
| `DB_NAME`, `DB_USER` | `videos`, `video_user` | database and user |
| `DB_PASSWORD` | **required** | password (from `.env`); `DB_PASSWORD_FILE` reads it from a file (Docker secrets) |
| `DB_POOL_MAX` | `10` | max connections per worker process |
| `DB_CONNECT_RETRIES`, `DB_CONNECT_DELAY` | `30`, `1` s | waiting for the database |
| `UPLOAD_DIR` | `./uploads` (`/app/uploads` in the image) | where videos are stored |
| `MAX_UPLOAD_MB` | `500` | upload size limit |

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | **UploadVideo**: multipart `file` + `company_name`; stores the file, upserts the record, returns `{"company_name", "video_url"}` |
| GET | `/get_video_url?company_name=...` | **GetVideoURL**: returns `{"company_name", "video_url"}`, `404` if unknown |
| GET | `/media/<filename>` | serves an uploaded video (plays in a browser) |
| GET | `/videos` | lists all records |
| GET | `/health` | liveness + database check |

### Database

PostgreSQL table `videos(id, company_name UNIQUE, file_path, video_url, created_at, updated_at)`,
created automatically on startup. Only the absolute file path and the URL are stored, never the
video bytes. Uploading the same `company_name` again updates the existing row (`INSERT ... ON
CONFLICT (company_name) DO UPDATE`, no duplicates).

### Behavior notes

- The video URL is built from the request's `Host`, not hard-coded.
- The stored file name is derived from the company name (`ACME GmbH` -> `ACME_GmbH.webm`). If two
  different names sanitize to the same file name, a short hash is appended so one video never
  overwrites another (uploads that could clash are serialized with an advisory lock).
- Accepted file types: `.webm`, `.mp4`, `.mov`, `.mkv`, `.ogv` (anything else returns `400`).
- Each worker process keeps a thread-safe connection pool (psycopg2); requests wait for a free
  connection instead of failing when the pool is busy. Schema creation is guarded by an advisory
  lock so that several workers can start at the same time.

## Recorder and preview (runs on the host)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

python capture_video.py                                  # input.csv -> ./video
python capture_video.py --headed                         # show the recording browser
python capture_video.py --server http://localhost:8000   # record, upload, preview in Chromium
python capture_video.py --server http://localhost:8000 --view-seconds 10
python capture_video.py --server http://localhost:8000 --view-seconds 0   # until Enter
```

Each video is saved as `<video folder>/<Company Name>.webm`. With `--server` every video is
uploaded, the returned URL is logged and opened in Chromium (`headless=False`) for
`--view-seconds` seconds (default 5; `0` waits for Enter). A failed row, upload or preview is
logged and the batch continues.

Input CSV: UTF-8, `;` delimiter, `"` qualifier, header
`URL;Company Name;Language;Country;Postcode;City;Street;HouseNo`.

`requirements.txt` is for local use (recorder + client + service); the Docker image installs only
`requirements-web.txt`.

## Files

| File | Purpose |
|---|---|
| `Dockerfile`, `docker-compose.yml` | image and the two-service stack |
| `.env.example` | template for `.env` (`DB_NAME`, `DB_USER`, `DB_PASSWORD`) |
| `web_service.py` | FastAPI service, PostgreSQL layer |
| `video_client.py` | `UploadVideo(...)`, `GetVideoURL(...)` |
| `capture_video.py`, `qscript.py` | recorder |
| `requirements-web.txt`, `requirements.txt` | service-only / full dependencies |

## Design notes

- **FastAPI is an ASGI framework**, so the container runs it with uvicorn directly instead of
  Apache + mod_wsgi (WSGI). This avoids an ASGI-to-WSGI adapter and keeps lifespan events,
  streaming uploads and `FileResponse` working as designed. Apache with a reverse proxy can be
  put in front of `web` later without changing the application.
- Host port `8000` maps to port `8000` of the container (uvicorn), not to Apache's port 80.
- Uploaded videos are served by the application (`/media/...`); for a larger deployment they
  would be served by a proxy or object storage.
- The database password is not in git: `.env` is ignored, `.env.example` holds a placeholder, and
  compose refuses to start without `DB_PASSWORD`. For production use Docker secrets
  (`DB_PASSWORD_FILE` is supported by the application).
- DuckDuckGo may answer automated recorder sessions with HTTP 418 or a captcha depending on the
  network; the recorder then returns `None` for that row and continues.
