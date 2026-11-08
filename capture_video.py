"""capture_video.py - read input.csv and record one video per row.

Usage:
    python capture_video.py                       # input.csv -> ./video
    python capture_video.py data.csv --out ./out  # custom CSV / video folder
    python capture_video.py --headed              # show the recording browser (debugging)

    # record, upload to the web service, preview the uploaded video in Chromium
    python capture_video.py --server http://127.0.0.1:8000
    python capture_video.py --server http://127.0.0.1:8000 --view-seconds 10
    python capture_video.py --server http://127.0.0.1:8000 --view-seconds 0   # until Enter
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
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
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
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


def upload_and_preview(server: str, video_path: str, company: str, view_seconds: float) -> bool:
    """Upload + preview. Never raises; returns False if the upload failed."""
    if not company:
        logger.error("cannot upload %s: the company name is empty", video_path)
        return False
    try:
        from video_client import UploadVideo  # lazy: `requests` is needed only with --server

        url = UploadVideo(server, video_path, company)
    except Exception as exc:  # noqa: BLE001 - a failed upload must not stop the batch
        logger.error("upload of %r failed: %s", company, exc)
        return False
    logger.info("uploaded %r -> %s", company, url)

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
        if args.server and not upload_and_preview(args.server, path, company, args.view_seconds):
            upload_failed += 1

    summary = f"Done: {len(rows) - failed} ok, {failed} failed"
    if args.server:
        summary += f"; uploaded {recorded - upload_failed}/{recorded}"
    print(summary)
    return 1 if (failed or upload_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
