# Browser video recorder + video web service

Two parts that work together:

1. **Recorder** (`capture_video.py`, `qscript.py`): for every row of a CSV file it records a
   `.webm` video of an automated browser session (Playwright + Chromium) with a custom SVG
   cursor, yellow highlight, click ripples and a callout.
2. **Web service** (`web_service.py`, `video_client.py`): uploads each recorded video, stores
   it in SQLite, looks it up by company name and serves it, so it can be previewed in a browser.

## Files

| File | Purpose |
|---|---|
| `qscript.py` | `CaptureVideo_Magento(...)` scenario, `PATH_TO_VIDEO_FOLDER`, `ShowCallout(...)` |
| `capture_video.py` | CSV loop; with `--server` it also uploads and previews every video |
| `web_service.py` | FastAPI service + SQLite layer (`videos.db`, `uploads/`) |
| `video_client.py` | `UploadVideo(...)`, `GetVideoURL(...)` (uses `requests`, imported lazily) |
| `input.csv` | sample input |
| `requirements.txt` | dependencies |

## Requirements

Python 3.9+ (developed and tested on 3.12). Newer `uvicorn` releases need Python 3.10+;
on 3.9 `pip` picks a compatible older version automatically.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 1. Recorder

```bash
python capture_video.py                       # input.csv -> ./video
python capture_video.py data.csv --out ./out  # custom CSV / output folder
python capture_video.py --headed              # show the recording browser (debugging)
```

Each video is saved as `<video folder>/<Company Name>.webm` (a row with the same name
overwrites the previous file) and its path is printed. A failed row does not stop the batch.

### Input format

UTF-8, `;` delimiter, `"` qualifier. Header:

```
URL;Company Name;Language;Country;Postcode;City;Street;HouseNo
```

The sample scenario uses `Company Name` (file name) and `City` (search query
`Magento developer in <city>`); the other columns are accepted but unused.

## 2. Web service

Start it (listens on `0.0.0.0:8000` by default):

```bash
python web_service.py
FLASK_HOST=127.0.0.1 FLASK_PORT=9000 python web_service.py   # custom host / port
```

On first start it creates `videos.db` and `uploads/` next to the script. Interactive API
docs (Swagger UI): `http://127.0.0.1:8000/docs`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | **UploadVideo**: multipart `file` + `company_name`; stores the file, upserts the record, returns `{"company_name", "video_url"}` |
| GET | `/get_video_url?company_name=...` | **GetVideoURL**: returns `{"company_name", "video_url"}`, `404` if unknown |
| GET | `/media/<filename>` | serves an uploaded video (plays in a browser) |
| GET | `/videos` | lists all records |

```bash
curl -F "file=@video/ACME GmbH.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload
# {"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
```

### Database

SQLite (`videos.db`, WAL mode). Table `videos(id, company_name UNIQUE, file_path, video_url,
created_at, updated_at)`. Only the absolute file path and the URL are stored, never the video
bytes. Uploading the same `company_name` again updates the existing row (upsert, no duplicates).

### Behavior notes

- The video URL is built from the request's host, not hard-coded.
- The stored file name is derived from the company name (`ACME GmbH` -> `ACME_GmbH.webm`). If two
  different names sanitize to the same file name, a short hash is appended so one video never
  overwrites another.
- Accepted file types: `.webm`, `.mp4`, `.mov`, `.mkv`, `.ogv` (anything else returns `400`).
  Upload size limit: `MAX_UPLOAD_MB` (default 500).
- Each request opens its own SQLite connection; writes use `BEGIN IMMEDIATE` with a busy timeout.

## 3. Record, upload and preview

With the service running in another terminal:

```bash
python capture_video.py --server http://127.0.0.1:8000
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 10
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 0   # until Enter
```

After each recorded video the script uploads it, logs the returned URL and opens it in
Chromium (`headless=False`). `--view-seconds N` keeps the preview open for `N` seconds
(default 5); `0` keeps it open until Enter is pressed. A failed upload or preview is logged and
the loop continues with the next row.

The client functions can also be used directly:

```python
from video_client import UploadVideo, GetVideoURL

url = UploadVideo("http://127.0.0.1:8000", "video/ACME GmbH.webm", "ACME GmbH")
assert GetVideoURL("http://127.0.0.1:8000", "ACME GmbH") == url
```

## Notes

- The web service is implemented with **FastAPI** instead of Flask.
  The `FLASK_HOST` / `FLASK_PORT` variable names are kept as specified in the task.
- DuckDuckGo may answer automated sessions with HTTP 418 or a captcha, depending on the
  network (VPN, datacenter IPs, repeated runs). In that case the recorder returns `None` for
  the row and the batch continues.
