#!/usr/bin/env python3
"""enrich_companies.py - fill companies from their Impressum pages via DeepSeek.

Standalone program (no web server): it iterates the companies table, opens
each company's home page, finds the Impressum/Imprint link, extracts the
visible text, prunes it to the legally relevant block (token budget) and asks
DeepSeek for structured JSON, then writes the result and a status code back to
PostgreSQL.

Usage::

    python enrich_companies.py                      # pending / non-OK rows
    python enrich_companies.py --company "ACME GmbH"
    python enrich_companies.py --limit 5 --status 1

Configuration comes from the environment (a ``.env`` file is read too):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD   PostgreSQL
    DEEPSEEK_API_KEY                                   required
    DEEPSEEK_MODEL, DEEPSEEK_BASE_URL                  optional
    HTTP_TIMEOUT, HTTP_DELAY_SEC, HTTP_USER_AGENT      polite crawling
    ENRICH_MOCK=1                                      offline demo/tests: a deterministic
                                                       fake DeepSeek answer is used and no
                                                       API key is needed
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

import companies as company_data
import deepseek_client
import impressum
from companies import Status
from deepseek_client import DeepSeekConfig, DeepSeekError
from impressum import ImpressumError, OptimizationResult

logger = logging.getLogger("enrich")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

#: Columns written back after every processed company (§4.6).
UPDATE_SQL = """
UPDATE companies SET
    status = %s,
    impressum_url = %s,
    legal_name = %s,
    postal_address = %s,
    handelsregister = %s,
    vat_number = %s,
    phone = %s,
    email = %s,
    responsible_person = %s,
    raw_json = %s,
    updated_at = NOW()
WHERE id = %s
"""

SELECT_SQL = (
    "SELECT id, company_name, web, status FROM companies "
    "{where} ORDER BY id {limit}"
)


@dataclass(frozen=True)
class HttpSettings:
    timeout: float = 20.0
    delay: float = 0.5
    user_agent: str = DEFAULT_USER_AGENT


def http_settings_from_env(args: Optional[argparse.Namespace] = None) -> HttpSettings:
    def value(name: str, arg: Any, default: Any) -> Any:
        return arg if arg is not None else os.environ.get(name, default)

    return HttpSettings(
        timeout=float(value("HTTP_TIMEOUT", getattr(args, "timeout", None), 20.0)),
        delay=float(value("HTTP_DELAY_SEC", getattr(args, "delay", None), 0.5)),
        user_agent=str(value("HTTP_USER_AGENT", None, DEFAULT_USER_AGENT)),
    )


def decide_status(extracted: Dict[str, Any]) -> Status:
    """Status of a finished extraction, in the order of §4.6."""
    if not deepseek_client.has_any_field(extracted):
        return Status.PARSING_FAILED
    if not deepseek_client.address_complete(extracted):
        return Status.ADDRESS_INCOMPLETE
    return Status.OK


def join_address(extracted: Dict[str, Any]) -> Optional[str]:
    """Human-readable postal address for the ``postal_address`` column."""
    address = extracted.get("postal_address") or {}
    street = " ".join(
        part for part in (address.get("street"), address.get("house_no")) if part
    )
    locality = " ".join(
        part for part in (address.get("postcode"), address.get("city")) if part
    )
    parts = [part for part in (street, locality, address.get("country")) if part]
    return ", ".join(parts) if parts else None


def map_extraction(
    extracted: Dict[str, Any], csv_company_name: str
) -> Dict[str, Any]:
    """JSON answer -> database columns (fallbacks per §6)."""
    return {
        "legal_name": extracted.get("company_name") or csv_company_name,
        "postal_address": join_address(extracted),
        "handelsregister": extracted.get("handelsregister"),
        "vat_number": extracted.get("vat_number"),
        "phone": extracted.get("phone"),
        "email": (extracted.get("email") or "").lower() or None,
        "responsible_person": extracted.get("responsible_person"),
        "raw_json": extracted,
    }


def build_update_params(
    status: Status,
    impressum_url: Optional[str],
    extracted: Optional[Dict[str, Any]],
    csv_company_name: str,
    company_id: int,
) -> tuple:
    values = map_extraction(extracted or {}, csv_company_name)
    return (
        int(status),
        impressum_url,
        values["legal_name"],
        values["postal_address"],
        values["handelsregister"],
        values["vat_number"],
        values["phone"],
        values["email"],
        values["responsible_person"],
        # psycopg2 does not adapt dicts to JSONB; serialize explicitly.
        json.dumps(extracted, ensure_ascii=False) if extracted is not None else None,
        company_id,
    )


def select_rows(
    cur: Any, company: Optional[str], limit: Optional[int], status: Optional[int]
) -> List[Dict[str, Any]]:
    conditions: List[str] = []
    params: List[Any] = []
    if company:
        conditions.append("company_name = %s")
        params.append(company)
    if status is not None:
        conditions.append("status = %s")
        params.append(status)
    else:
        conditions.append("(status IS NULL OR status <> 0)")
    where = "WHERE " + " AND ".join(conditions)
    sql = SELECT_SQL.format(where=where, limit="LIMIT %s" if limit is not None else "")
    if limit is not None:
        params.append(limit)
    cur.execute(sql, tuple(params))
    return [dict(row) for row in cur.fetchall()]


@dataclass
class Outcome:
    status: Status
    reason: str
    impressum_url: Optional[str] = None
    extraction: Optional[Dict[str, Any]] = None
    optimization: Optional[OptimizationResult] = None


def _fetch(session: requests.Session, url: str, settings: HttpSettings) -> requests.Response:
    response = session.get(
        url,
        timeout=settings.timeout,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "de-DE,de;q=0.9"},
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def enrich_company(
    row: Dict[str, Any],
    settings: HttpSettings,
    deepseek: DeepSeekConfig,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    extractor: Callable[..., Dict[str, Any]] = deepseek_client.extract_impressum,
) -> Outcome:
    name = row.get("company_name") or ""
    web = impressum.normalize_url(row.get("web"))
    if web is None:
        return Outcome(Status.NO_WEBSITE, "web column is empty or not a web URL")

    http = session or requests.Session()
    try:
        home = _fetch(http, web, settings)
        html = home.text
        home_url = home.url or web
        logger.info("%s: home page fetched from %s", name, home_url)

        link = impressum.find_impressum_link(html, home_url)
        if link is None:
            return Outcome(
                Status.NO_IMPRESSUM_LINK_FOUND,
                f"no Impressum/Imprint link on {home_url}",
            )
        logger.info("%s: Impressum link %s", name, link)

        if settings.delay:
            sleep(settings.delay)
        page = _fetch(http, link, settings)
        impressum_url = page.url or link
    except requests.RequestException as exc:
        return Outcome(Status.ERROR, f"HTTP error: {exc}")
    except ImpressumError as exc:
        return Outcome(Status.ERROR, str(exc))

    try:
        text = impressum.extract_visible_text(page.text)
    except ImpressumError as exc:
        return Outcome(Status.ERROR, str(exc), impressum_url=impressum_url)
    if not text.strip():
        return Outcome(
            Status.PARSING_FAILED,
            "Impressum page has no visible text",
            impressum_url=impressum_url,
        )

    optimization = impressum.optimize_text(text)
    logger.info("%s: %s", name, impressum.log_summary(optimization))

    try:
        extracted = extractor(optimization.text, deepseek, session=http, sleep=sleep)
    except DeepSeekError as exc:
        return Outcome(
            Status.ERROR,
            f"DeepSeek error: {exc}",
            impressum_url=impressum_url,
            optimization=optimization,
        )
    status = decide_status(extracted)
    return Outcome(
        status,
        f"{status.name}: fields detected={deepseek_client.has_any_field(extracted)}, "
        f"address complete={deepseek_client.address_complete(extracted)}",
        impressum_url=impressum_url,
        extraction=extracted,
        optimization=optimization,
    )


# Mocks for testing 

#: German city prefix; used only by the offline mock extractor.
_MOCK_CITY = r"\b(?:\d{5}\s+)?([A-ZÄÖÜ][a-zäöüß-]{2,}(?:\s[A-ZÄÖÜ][a-zäöüß-]+)?)\b"
_MOCK_CITY_HINTS = ("berlin", "munich", "münchen", "hamburg", "cologne", "köln", "frankfurt")


def _mock_city(text: str) -> Optional[str]:
    for line in text.splitlines():
        folded = line.lower()
        if not any(hint in folded for hint in _MOCK_CITY_HINTS):
            continue
        match = re.search(r"\b\d{5}\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+(?:\s[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)?)", line)
        if match:
            return match.group(1)
    return None


def mock_extract(text: str, config: Any = None, session: Any = None, sleep: Any = None) -> Dict[str, Any]:
    """Deterministic stand-in for :func:`deepseek_client.extract_impressum`.

    ``ENRICH_MOCK=1`` makes the enricher runnable without an API key (demo,
    CI).  It intentionally implements only a tiny fraction of the real
    extraction: an optional leading "MOCK_JSON:" line contains the exact
    answer, otherwise the text is scanned with a few patterns.
    """
    if text.startswith("MOCK_JSON:"):
        return json.loads(text.split("\n", 1)[1])
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    phone = re.search(r"\+49[\d\s\-/()]{6,}", text)
    vat = re.search(r"\bDE\s?\d{9}\b", text)
    postcode = re.search(r"\b\d{5}\b", text)
    street = re.search(r"([A-ZÄÖÜ][\wäöüß.\- ]+?\s(?:Straße|strasse|Street|Weg|Platz|Allee)\s*\d+[a-z]?)", text)
    name = re.search(r"([A-ZÄÖÜ][\wäöüß&.\- ]{2,60}?(?:GmbH|AG|KG|UG|Ltd|Inc)\.?)", text)
    street_part, house_no = (street.group(1).rsplit(" ", 1) if street else (None, None))
    return deepseek_client.normalize_extraction(
        {
            "company_name": name.group(1) if name else None,
            "postal_address": {
                "street": street_part,
                "house_no": house_no,
                "postcode": postcode.group(0) if postcode else None,
                "city": _mock_city(text),
                "country": "Germany",
            },
            "vat_number": vat.group(0) if vat else None,
            "phone": phone.group(0).strip() if phone else None,
            "email": email.group(0).lower() if email else None,
        }
    )


def is_mock_mode() -> bool:
    return os.environ.get("ENRICH_MOCK", "").strip().lower() in ("1", "true", "yes")


def process_rows(
    rows: Sequence[Dict[str, Any]],
    settings: HttpSettings,
    deepseek: DeepSeekConfig,
    connection_factory: Callable[[], Any],
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    extractor: Callable[..., Dict[str, Any]] = deepseek_client.extract_impressum,
) -> Dict[str, int]:
    """Enrich rows one by one and write the result back; returns status counts."""
    counts: Dict[str, int] = {}
    http = session or requests.Session()
    for row in rows:
        outcome = enrich_company(
            row, settings, deepseek, session=http, sleep=sleep, extractor=extractor
        )
        level = logging.INFO if outcome.status == Status.OK else logging.WARNING
        logger.log(
            level,
            "%s -> %s (%s)",
            row.get("company_name"),
            outcome.status.name,
            outcome.reason,
        )
        params = build_update_params(
            outcome.status,
            outcome.impressum_url,
            outcome.extraction,
            str(row.get("company_name") or ""),
            int(row["id"]),
        )
        with connection_factory() as conn, conn.cursor() as cur:
            cur.execute(UPDATE_SQL, params)
            conn.commit()
        counts[outcome.status.name] = counts.get(outcome.status.name, 0) + 1
    return counts


def _status_arg(value: str) -> int:
    text = value.strip()
    if text.lower() in ("null", "pending"):
        raise argparse.ArgumentTypeError(
            "NULL means 'not yet processed'; use --status with a code 0-5 "
            "(the default already selects pending and non-OK rows)"
        )
    try:
        code = int(text)
    except ValueError:
        try:
            return int(Status[text.upper()])
        except KeyError:
            raise argparse.ArgumentTypeError(f"unknown status {value!r}") from None
    if code not in {int(status) for status in Status}:
        raise argparse.ArgumentTypeError(f"status must be 0-5, got {code}")
    return code


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return number


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich company rows from their Impressum pages with DeepSeek."
    )
    parser.add_argument("--company", default=None, help="process only this company_name")
    parser.add_argument("--limit", type=_positive_int, default=None, help="max rows")
    parser.add_argument(
        "--status",
        type=_status_arg,
        default=None,
        help="process only rows with this status (default: pending and status <> 0)",
    )
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout per request")
    parser.add_argument("--delay", type=float, default=None, help="pause between HTTP requests")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="enrich and print the results without writing to the database",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    mock_mode = is_mock_mode()
    if mock_mode:
        logger.warning("ENRICH_MOCK=1: using a deterministic fake DeepSeek answer")
    deepseek = deepseek_client.config_from_env()
    if not mock_mode and not deepseek.api_key:
        logger.error("DEEPSEEK_API_KEY is not set (environment or .env)")
        return 2
    if not math.isfinite(deepseek.timeout) or deepseek.timeout <= 0:
        logger.error("invalid DeepSeek timeout")
        return 2

    settings = http_settings_from_env(args)
    # Import lazily: importing the web service needs the database driver only
    # when the script actually talks to PostgreSQL.
    import web_service

    try:
        web_service.init_db()  # also creates companies on first use
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot connect to the database: %s", exc)
        return 2

    try:
        with web_service.db_connection() as conn, conn.cursor() as cur:
            rows = select_rows(cur, args.company, args.limit, args.status)
    finally:
        web_service.close_pool()

    if not rows:
        logger.info("nothing to process")
        return 0
    logger.info("processing %d company/companies", len(rows))

    factory = _null_connection_factory if args.dry_run else web_service.db_connection
    extractor = mock_extract if mock_mode else deepseek_client.extract_impressum
    counts = process_rows(rows, settings, deepseek, factory, extractor=extractor)
    logger.info("done: %s", ", ".join(f"{name}={count}" for name, count in counts.items()))
    return 0 if counts.get(Status.ERROR.name, 0) == 0 else 1


@contextmanager
def _null_connection_factory():  # pragma: no cover - CLI helper
    """A dry-run stand-in that ignores every UPDATE."""

    class _Cursor:
        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _Conn:
        def cursor(self) -> "_Cursor":
            return _Cursor()

        def commit(self) -> None:
            return None

        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

    yield _Conn()


if __name__ == "__main__":
    raise SystemExit(main())
