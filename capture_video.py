"""capture_video.py - read input.csv and record one video per row.

Usage:
    python capture_video.py                       # input.csv -> ./video
    python capture_video.py data.csv --out ./out  # custom CSV / video folder
    python capture_video.py --headed              # show the browser window (debugging)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from typing import Dict, List

import qscript

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record a browser-automation video for every CSV row.")
    parser.add_argument("csv_path", nargs="?", default="input.csv", help="input CSV (default: input.csv)")
    parser.add_argument("--out", default=None, help="video folder (default: qscript.PATH_TO_VIDEO_FOLDER)")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    return parser.parse_args()


def read_rows(path: str) -> List[Dict[str, str]]:
    # utf-8-sig also accepts files saved by Excel with a BOM.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        missing = [c for c in EXPECTED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("CSV is missing columns: %s" % ", ".join(missing))
        return list(reader)


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
        print("ERROR: cannot read %s: %s" % (args.csv_path, exc), file=sys.stderr)
        return 2

    failed = 0
    for index, row in enumerate(rows, start=1):
        def field(name: str) -> str:
            return (row.get(name) or "").strip()

        company = field("Company Name")
        label = f"[{index}/{len(rows)}] {company or '(no name)'}"
        if not field("URL"):
            print("%s -> SKIPPED (empty URL)" % label)
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
            print("%s -> FAILED (%s)" % (label, exc))
            failed += 1
            continue

        if path:
            print("%s -> %s" % (label, path))
        else:
            print("%s -> FAILED" % label)
            failed += 1

    print("Done: %d ok, %d failed" % (len(rows) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
