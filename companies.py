"""companies.py - the companies table shared by the web service and the enricher.

Imported company data is being stored in PostgreSQL and enriched
from the company's Impressum page.  This module keeps the table definition,
the status codes and the small helpers both the FastAPI service and
``enrich_companies.py`` need.  It imports neither FastAPI nor beautifulsoup4,
so it stays easy to unit-test.
"""

from __future__ import annotations

import csv
import io
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ENV_COLUMNS: Tuple[str, ...] = (
    "language",
    "country",
    "postcode",
    "city",
    "street",
    "house_no",
    "web",
)

REQUIRED_CSV_COLUMNS: Tuple[str, ...] = ("Company Name", "Web")

SCHEMA_FILE = Path(__file__).resolve().with_name("schema.sql")


class Status(IntEnum):
    """``companies.status`` values (NULL means "pending"; see Status.NULL)."""

    OK = 0
    NO_IMPRESSUM_LINK_FOUND = 1
    PARSING_FAILED = 2
    ADDRESS_INCOMPLETE = 3
    ERROR = 4
    NO_WEBSITE = 5

    def __str__(self) -> str:
        return self.name.lower()

    @classmethod
    def name_of(cls, value: Optional[int]) -> str:
        """Human-readable name of a stored status (None -> "pending")."""
        if value is None:
            return "pending"
        try:
            return cls(value).name
        except ValueError:
            return "unknown"

    @classmethod
    def is_ok(cls, value: Optional[int]) -> bool:
        return value is not None and int(value) == int(cls.OK)


#: NULL is the "pending" status; kept as a named alias for readability.
PENDING: Optional[Status] = None


class CompaniesError(ValueError):
    """The CSV or the requested operation is invalid."""


def load_schema_sql() -> str:
    """The idempotent DDL of the companies table (schema.sql)."""
    return SCHEMA_FILE.read_text(encoding="utf-8")


def _clean(value: Any) -> Optional[str]:
    """CSV cell -> stripped string, '' -> None (blank means "not provided")."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_companies_csv(data: bytes) -> List[Dict[str, Optional[str]]]:
    """Parse companies.csv into rows ready for an upsert.

    UTF-8 (a BOM is tolerated), ``;`` delimiter, ``"`` qualifier, per the task.
    ``Company Name`` is required and a header without ``Web`` means "no
    website" for every row (it is imported and later marked NO_WEBSITE by the
    enricher).  Header names are matched case-insensitively and trimmed.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CompaniesError(f"CSV is not valid UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text), delimiter=";", quotechar='"')
    if reader.fieldnames is None:
        raise CompaniesError("CSV has no header row")
    header = {(name or "").strip().lower(): name for name in reader.fieldnames}
    missing = [column for column in REQUIRED_CSV_COLUMNS if column.lower() not in header]
    if "company name" in [column.lower() for column in missing]:
        raise CompaniesError("CSV is missing the required column: Company Name")
    has_web = "web" in header

    rows: List[Dict[str, Optional[str]]] = []
    for raw in reader:
        by_header = {
            (key or "").strip().lower(): value for key, value in raw.items() if key is not None
        }

        def field(column: str) -> Optional[str]:
            return _clean(by_header.get(column.lower()))

        company = field("Company Name")
        if not company:
            # A blank key cannot be upserted; report the CSV line number.
            raise CompaniesError(
                f"row {reader.line_num}: Company Name must not be empty"
            )
        row: Dict[str, Optional[str]] = {
            "company_name": company,
            "language": field("Language"),
            "country": field("Country"),
            "postcode": field("Postcode"),
            "city": field("City"),
            "street": field("Street"),
            "house_no": field("HouseNo"),
            "web": field("Web") if has_web else None,
        }
        rows.append(row)
    return rows


#: CSV-provided columns updated by a re-import; status and extracted fields are
#: deliberately absent so that enrichment results survive a re-import.
UPSERT_CSV_SQL = """
INSERT INTO companies (company_name, language, country, postcode, city, street, house_no, web)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (company_name) DO UPDATE SET
    language = EXCLUDED.language,
    country = EXCLUDED.country,
    postcode = EXCLUDED.postcode,
    city = EXCLUDED.city,
    street = EXCLUDED.street,
    house_no = EXCLUDED.house_no,
    web = EXCLUDED.web,
    updated_at = NOW()
RETURNING (xmax = 0) AS inserted
"""


def upsert_companies(cur: Any, rows: Sequence[Dict[str, Optional[str]]]) -> Tuple[int, int]:
    """Upsert parsed rows; returns ``(imported, updated)``.

    Uses the caller's transaction/cursor.  ``(xmax = 0)`` is the standard
    PostgreSQL way to tell an INSERT from an UPDATE in ``RETURNING``.
    """
    imported = 0
    updated = 0
    for row in rows:
        cur.execute(
            UPSERT_CSV_SQL,
            (
                row["company_name"],
                row.get("language"),
                row.get("country"),
                row.get("postcode"),
                row.get("city"),
                row.get("street"),
                row.get("house_no"),
                row.get("web"),
            ),
        )
        result = cur.fetchone()
        if result is not None and result["inserted"]:
            imported += 1
        else:
            updated += 1
    return imported, updated


READ_SQL = (
    "SELECT id, company_name, language, country, postcode, city, street, house_no, "
    "web, status, impressum_url, legal_name, postal_address, handelsregister, "
    "vat_number, phone, email, responsible_person, raw_json, created_at, updated_at "
    "FROM companies"
)


def row_to_json(row: Dict[str, Any]) -> Dict[str, Any]:
    """Database row -> API object (adds the symbolic status name)."""
    data = dict(row)
    data["status_name"] = Status.name_of(data.get("status"))
    return data


def select_companies(cur: Any, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """All companies ordered by id; ``status`` filters by code or by name.

    ``status="null"`` / ``"pending"`` selects rows that were imported but never
    processed; numeric codes select that status; a symbolic name such as
    ``"ok"`` is also accepted.
    """
    if status is None:
        cur.execute(READ_SQL + " ORDER BY id")
        return [row_to_json(row) for row in cur.fetchall()]

    text = status.strip()
    if text.lower() in ("null", "pending"):
        cur.execute(READ_SQL + " WHERE status IS NULL ORDER BY id")
    elif text.lstrip("-").isdigit():
        cur.execute(READ_SQL + " WHERE status = %s ORDER BY id", (int(text),))
    else:
        try:
            code = int(Status[text.upper()])
        except KeyError as exc:
            raise CompaniesError(
                f"unknown status {status!r}: use NULL, a code 0-5 or a name such as OK"
            ) from exc
        cur.execute(READ_SQL + " WHERE status = %s ORDER BY id", (code,))
    return [row_to_json(row) for row in cur.fetchall()]


def select_company(cur: Any, company_name: str) -> Optional[Dict[str, Any]]:
    cur.execute(READ_SQL + " WHERE company_name = %s", (company_name.strip(),))
    row = cur.fetchone()
    return row_to_json(row) if row is not None else None
