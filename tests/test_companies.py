"""companies.py: CSV parsing, status codes and JSON helpers."""

import pytest

import companies
from companies import CompaniesError, Status, parse_companies_csv, row_to_json


HEADER = "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web\n"


def test_status_codes_match_the_task():
    assert int(Status.OK) == 0
    assert int(Status.NO_IMPRESSUM_LINK_FOUND) == 1
    assert int(Status.PARSING_FAILED) == 2
    assert int(Status.ADDRESS_INCOMPLETE) == 3
    assert int(Status.ERROR) == 4
    assert int(Status.NO_WEBSITE) == 5


def test_status_name_for_pending_and_names():
    assert Status.name_of(None) == "pending"
    assert Status.name_of(0) == "OK"
    assert Status.name_of(1) == "NO_IMPRESSUM_LINK_FOUND"
    assert Status.name_of(99) == "unknown"
    assert Status.is_ok(0) and not Status.is_ok(None) and not Status.is_ok(3)


def test_parse_sample_csv():
    rows = parse_companies_csv(
        (
            HEADER
            + "https://duckduckgo.com;ACME GmbH;German;Germany;10115;Berlin;Main Street;12;"
            "https://www.acme-gmbh.de\n"
        ).encode("utf-8")
    )
    assert rows == [
        {
            "company_name": "ACME GmbH",
            "language": "German",
            "country": "Germany",
            "postcode": "10115",
            "city": "Berlin",
            "street": "Main Street",
            "house_no": "12",
            "web": "https://www.acme-gmbh.de",
        }
    ]


def test_parse_quoted_semicolon_and_utf8_bom():
    data = "\ufeff" + HEADER + 'https://x.de;"Smith; Jones Ltd";English;;;;;;www.x.de\n'
    rows = parse_companies_csv(data.encode("utf-8"))
    assert rows[0]["company_name"] == "Smith; Jones Ltd"
    assert rows[0]["web"] == "www.x.de"
    assert rows[0]["city"] is None


def test_web_is_optional():
    data = HEADER + "https://x.de;No Web Ltd;German;;;;;;\n"
    rows = parse_companies_csv(data.encode("utf-8"))
    assert rows[0]["web"] is None


def test_missing_web_column_means_no_website():
    data = "URL;Company Name;Language\nhttps://x.de;ACME;German\n"
    rows = parse_companies_csv(data.encode("utf-8"))
    assert rows[0]["web"] is None


def test_header_is_case_insensitive():
    data = "url;company name;WEB\nhttps://x.de;ACME GmbH;acme.de\n"
    rows = parse_companies_csv(data.encode("utf-8"))
    assert rows[0]["company_name"] == "ACME GmbH"
    assert rows[0]["web"] == "acme.de"


def test_missing_company_name_column_is_rejected():
    with pytest.raises(CompaniesError, match="Company Name"):
        parse_companies_csv(b"URL;Web\nhttps://x.de;x.de\n")


def test_blank_company_name_row_is_rejected_with_line_number():
    data = HEADER + "https://x.de;ACME;German;;;;;;\nhttps://y.de;  ;German;;;;;;\n"
    with pytest.raises(CompaniesError, match="row 3"):
        parse_companies_csv(data.encode("utf-8"))


def test_invalid_utf8_is_rejected():
    with pytest.raises(CompaniesError, match="UTF-8"):
        parse_companies_csv(b"URL;Company Name;Web\n\xff\xfe;ACME;acme.de\n")


def test_empty_csv_has_no_rows():
    assert parse_companies_csv(HEADER.encode("utf-8")) == []


def test_schema_sql_is_idempotent_and_has_all_columns():
    sql = companies.load_schema_sql()
    assert "CREATE TABLE IF NOT EXISTS companies" in sql
    for column in (
        "company_name",
        "language",
        "country",
        "postcode",
        "city",
        "street",
        "house_no",
        "web",
        "status",
        "impressum_url",
        "legal_name",
        "postal_address",
        "handelsregister",
        "vat_number",
        "phone",
        "email",
        "responsible_person",
        "raw_json",
        "created_at",
        "updated_at",
    ):
        assert column in sql
    assert "JSONB" in sql


def test_row_to_json_adds_status_name():
    row = {"id": 1, "company_name": "ACME", "status": None}
    assert row_to_json(row)["status_name"] == "pending"
    assert row_to_json({**row, "status": 0})["status_name"] == "OK"
