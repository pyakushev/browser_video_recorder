# CSV-driven browser automation video recorder

For every row of an input CSV, `capture_video.py` calls
`qscript.CaptureVideo_Magento(...)`, which records a `.webm` video of an
automated browser session (Playwright + Chromium): a custom SVG cursor with a
yellow highlight, smooth movement, human-like typing, a search, click ripples
and a callout, ending with a click on the first result.

## Requirements
Python 3.9+ (tested on 3.12).

## Setup
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Run
```bash
python capture_video.py                       # input.csv -> ./video
python capture_video.py data.csv --out ./out  # custom CSV / output folder
python capture_video.py --headed              # show the browser window
```
Videos are saved as `<video folder>/<Company Name>.webm`; a row with the same
name overwrites the previous file. The path of each video is printed.

## Input format
UTF-8, `;` delimiter, `"` qualifier. Header:
`URL;Company Name;Language;Country;Postcode;City;Street;HouseNo`

## Notes
- `qscript.PATH_TO_VIDEO_FOLDER` (default `./video`) can be overridden by the caller.
- Only `City` and `Company Name` are used by the sample scenario.
- DuckDuckGo may answer automated sessions with HTTP 418 or a captcha
  depending on the network (VPN, datacenter IPs, repeated runs). In that case
  the function returns `None` and the batch continues with the next row.
