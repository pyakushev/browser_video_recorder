"""capture_video.py - read input.csv and record one video per row.

Usage:
    python capture_video.py                       # input.csv -> ./video
    python capture_video.py data.csv --out ./out  # custom CSV / video folder
    python capture_video.py --headed              # show the recording browser (debugging)
    python capture_video.py --no-auto-zoom        # only F5 / window.__zoomAt trigger zoom
    python capture_video.py --zoom-factor 2.5 --zoom-ramp 0.4

    # record, upload to the web service, preview the uploaded video in Chromium
    python capture_video.py --server http://127.0.0.1:8000
    python capture_video.py --server http://127.0.0.1:8000 --view-seconds 10
    python capture_video.py --server http://127.0.0.1:8000 --view-seconds 0   # until Enter
    python capture_video.py --server http://127.0.0.1:8000 --wait-processed

A zoom sidecar is written next to the video only when at least one zoom was
triggered; with --server it is uploaded together with the video and the service
queues the post-processing automatically.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import sys
import time
from typing import Dict, List

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import qscript

logger = logging.getLogger("capture_video")

EXPECTED_COLUMNS = [
    "URL",
    "Company Name",
    "Language",
    "Country",
    "Postcode",
    "City",
    "Street",
    "HouseNo",
]

PREVIEW_HEADLESS = False  # the preview must be visible
PREVIEW_VIEWPORT = {"width": 1280, "height": 720}


def _non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}")
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return number


def _zoom_factor(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}")
    if not math.isfinite(number) or number <= 1.0:
        raise argparse.ArgumentTypeError("must be a finite number > 1.0")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record a browser-automation video for every CSV row.")
    parser.add_argument("csv_path", nargs="?", default="input.csv", help="input CSV (default: input.csv)")
    parser.add_argument("--out", default=None, help="video folder (default: qscript.PATH_TO_VIDEO_FOLDER)")
    parser.add_argument("--headed", action="store_true", help="show the recording browser window")
    parser.add_argument(
        "--server",
        default=None,
        metavar="URL",
        help="web service URL, e.g. http://127.0.0.1:8000; upload each video and preview it",
    )
    parser.add_argument(
        "--view-seconds",
        type=_non_negative_float,
        default=5.0,
        metavar="N",
        help="preview length: N > 0 keeps the browser open N seconds, "
        "0 keeps it open until Enter is pressed (default: 5)",
    )
    parser.add_argument(
        "--zoom-factor",
        type=_zoom_factor,
        default=None,
        metavar="N",
        help="zoom magnification written to the sidecar (default: 2.0)",
    )
    parser.add_argument(
        "--zoom-ramp",
        type=_non_negative_float,
        default=None,
        metavar="SEC",
        help="zoom-in/zoom-out duration written to the sidecar (default: 0.5)",
    )
    parser.add_argument(
        "--no-auto-zoom",
        action="store_true",
        help="do not trigger the demo zooms in the scenario; F5/__zoomAt still work",
    )
    parser.add_argument(
        "--wait-processed",
        action="store_true",
        help="after upload, wait for the server-side zoom and preview the processed video",
    )
    parser.add_argument(
        "--process-timeout",
        type=_non_negative_float,
        default=120.0,
        metavar="SEC",
        help="max wait for --wait-processed (default: 120)",
    )
    return parser.parse_args()


def read_rows(path: str) -> List[Dict[str, str]]:
    # utf-8-sig also accepts files saved by Excel with a BOM.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        missing = [c for c in EXPECTED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(missing)}")
        return list(reader)


def _wait_for_enter() -> None:
    try:
        input("Preview is open. Press Enter to close it... ")
    except EOFError:
        logger.warning("stdin is not interactive, closing the preview immediately")


def preview_video(url: str, view_seconds: float) -> None:
    """Open the uploaded video in Chromium (Chromium shows its media player for a .webm URL)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=PREVIEW_HEADLESS)
        try:
            page = browser.new_page(viewport=dict(PREVIEW_VIEWPORT))
            page.goto(url, wait_until="commit", timeout=30_000)
            try:
                if view_seconds > 0:
                    page.wait_for_timeout(view_seconds * 1000)
                else:
                    _wait_for_enter()
            except PlaywrightError as exc:
                if "closed" not in str(exc).lower():
                    raise
                logger.info("preview window was closed by the user")
        finally:
            browser.close()


def wait_for_processed(server: str, company: str, timeout: float) -> Dict[str, str]:
    """Poll the service until the zoom job finishes (or the timeout expires)."""
    from video_client import GetVideoInfo  # lazy: `requests` is needed only with --server

    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        try:
            info = GetVideoInfo(server, company)
        except Exception as exc:  # noqa: BLE001 - polling is best-effort
            logger.warning("cannot read processing status for %r: %s", company, exc)
            return {}
        if "zoom_status" not in info:
            return info  # an older server without zoom support
        status = info.get("zoom_status")
        if status in ("done", "failed", "none", "skipped", "cancelled"):
            if status == "failed":
                logger.error(
                    "server-side zoom failed for %r: %s", company, info.get("zoom_error")
                )
            return info
        if status != last_status:
            logger.info("processing %r: %s", company, status or "pending")
            last_status = status
        if time.monotonic() >= deadline:
            logger.warning("processing of %r did not finish in %.0f s", company, timeout)
            return info
        time.sleep(1.0)


def upload_and_preview(
    server: str,
    video_path: str,
    company: str,
    view_seconds: float,
    wait_processed: bool = False,
    process_timeout: float = 120.0,
) -> bool:
    """Upload + preview. Never raises; returns False if the upload failed."""
    if not company:
        logger.error("cannot upload %s: the company name is empty", video_path)
        return False

    sidecar = qscript.zoom_sidecar_path(video_path)
    if not os.path.isfile(sidecar):
        sidecar = None

    try:
        from video_client import UploadVideo  # lazy: `requests` is needed only with --server

        url = UploadVideo(server, video_path, company, sidecar_path=sidecar)
    except Exception as exc:  # noqa: BLE001 - a failed upload must not stop the batch
        logger.error("upload of %r failed: %s", company, exc)
        return False
    logger.info("uploaded %r -> %s", company, url)
    if sidecar:
        logger.info("uploaded zoom sidecar: %s", sidecar)

    if wait_processed and sidecar:
        info = wait_for_processed(server, company, process_timeout)
        processed = (info or {}).get("processed_video_url")
        if processed:
            url = processed
            logger.info("using processed video: %s", url)

    try:
        preview_video(url, view_seconds)
    except Exception as exc:  # noqa: BLE001 - a failed preview must not stop the batch
        logger.error("preview of %s failed: %s", url, exc)
    return True


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.out:
        qscript.PATH_TO_VIDEO_FOLDER = args.out  # override the module global at startup
    if args.headed:
        qscript.HEADLESS = False
    if args.zoom_factor is not None:
        qscript.ZOOM_FACTOR = args.zoom_factor
    if args.zoom_ramp is not None:
        qscript.ZOOM_RAMP_SEC = args.zoom_ramp
    if args.no_auto_zoom:
        qscript.AUTO_ZOOM = False

    try:
        rows = read_rows(args.csv_path)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"ERROR: cannot read {args.csv_path}: {exc}", file=sys.stderr)
        return 2

    failed = 0
    recorded = 0
    upload_failed = 0
    for index, row in enumerate(rows, start=1):

        def field(name: str) -> str:
            return (row.get(name) or "").strip()

        company = field("Company Name")
        label = f"[{index}/{len(rows)}] {company or '(no name)'}"
        if not field("URL"):
            print(f"{label} -> SKIPPED (empty URL)")
            failed += 1
            continue
        try:
            path = qscript.CaptureVideo_Magento(
                url=field("URL"),
                company_name=company,
                language=field("Language"),
                country=field("Country"),
                postcode=field("Postcode"),
                city=field("City"),
                street=field("Street"),
                house_no=field("HouseNo"),
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            print(f"{label} -> FAILED ({exc})")
            failed += 1
            continue

        if not path:
            print(f"{label} -> FAILED")
            failed += 1
            continue

        recorded += 1
        print(f"{label} -> {path}")
        if args.server and not upload_and_preview(
            args.server,
            path,
            company,
            args.view_seconds,
            wait_processed=args.wait_processed,
            process_timeout=args.process_timeout,
        ):
            upload_failed += 1

    summary = f"Done: {len(rows) - failed} ok, {failed} failed"
    if args.server:
        summary += f"; uploaded {recorded - upload_failed}/{recorded}"
    print(summary)
    return 1 if (failed or upload_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
