"""enrich_companies.py: status rules, mapping and the per-company pipeline."""

import json
import pytest
import requests

import enrich_companies as enrich
from companies import Status
from deepseek_client import DeepSeekConfig, DeepSeekError, normalize_extraction

HTTP = enrich.HttpSettings(timeout=1.0, delay=0.0)
CONFIG = DeepSeekConfig(api_key="test-key")

HOME = """<html><body>
  <a href="/impressum">Impressum</a>
</body></html>"""

IMPRESSUM = """<html><body><main>
  <p>Angaben gemäß § 5 TMG</p>
  <p>ACME GmbH, Main Street 12, 10115 Berlin</p>
  <p>Telefon: +49 30 1234567</p>
  <p>E-Mail: info@acme-gmbh.de</p>
</main></body></html>"""

GOOD = normalize_extraction(
    {
        "company_name": "ACME GmbH",
        "postal_address": {
            "street": "Main Street",
            "house_no": "12",
            "postcode": "10115",
            "city": "Berlin",
            "country": "Germany",
        },
        "vat_number": "DE123456789",
        "email": "Info@ACME.de",
    }
)


class FakeResponse:
    def __init__(self, status_code=200, text="", url="https://acme.de/", reason="OK"):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.reason = reason

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} {self.reason}", response=self)


class FakeHttp:
    """Replays pages by URL substring; a requests exception raises."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.calls.append(url)
        for needle, page in self.pages.items():
            if needle in url:
                if isinstance(page, Exception):
                    raise page
                return page
        raise requests.ConnectionError(f"unexpected URL {url}")


def row(**overrides):
    data = {"id": 1, "company_name": "ACME GmbH", "web": "acme.de", "status": None}
    data.update(overrides)
    return data


def extractor(answer):
    def _extract(text, config, session=None, sleep=None):
        _extract.last_text = text
        if isinstance(answer, Exception):
            raise answer
        return answer

    _extract.last_text = None
    return _extract


# --- status rules (§4.6) -----------------------------------------------------


def test_status_ok_when_fields_and_address_complete():
    assert enrich.decide_status(GOOD) is Status.OK


def test_status_parsing_failed_without_fields():
    assert enrich.decide_status(normalize_extraction({})) is Status.PARSING_FAILED


def test_status_address_incomplete():
    partial = normalize_extraction(
        {"company_name": "ACME", "postal_address": {"street": "Main Street"}}
    )
    assert enrich.decide_status(partial) is Status.ADDRESS_INCOMPLETE


def test_address_complete_ignores_missing_country():
    data = normalize_extraction(
        {
            "company_name": "ACME",
            "postal_address": {
                "street": "Main Street",
                "house_no": "12",
                "postcode": "10115",
                "city": "Berlin",
            },
        }
    )
    assert enrich.decide_status(data) is Status.OK


def test_missing_vat_or_phone_does_not_downgrade():
    data = normalize_extraction(
        {
            "company_name": "ACME",
            "postal_address": {
                "street": "S",
                "house_no": "1",
                "postcode": "10115",
                "city": "Berlin",
            },
        }
    )
    assert enrich.decide_status(data) is Status.OK


# --- mapping (§6) ------------------------------------------------------------


def test_join_address():
    assert enrich.join_address(GOOD) == "Main Street 12, 10115 Berlin, Germany"
    assert enrich.join_address(normalize_extraction({})) is None


def test_map_extraction_falls_back_to_csv_name_and_lowercases_email():
    mapped = enrich.map_extraction(
        normalize_extraction({"postal_address": {"street": "S"}}), "CSV GmbH"
    )
    assert mapped["legal_name"] == "CSV GmbH"
    assert mapped["email"] is None


def test_build_update_params_order_and_json():
    params = enrich.build_update_params(Status.OK, "https://acme.de/impressum", GOOD, "ACME GmbH", 7)
    assert params[0] == 0
    assert params[1] == "https://acme.de/impressum"
    assert params[2] == "ACME GmbH"
    assert params[-1] == 7
    assert json.loads(params[-2])["email"] == "Info@ACME.de"


def test_build_update_params_for_failure_keeps_url_and_nulls_fallback_name():
    params = enrich.build_update_params(Status.ERROR, "https://acme.de/impressum", None, "ACME GmbH", 9)
    assert params[0] == 4
    assert params[2] == "ACME GmbH"
    assert params[-2] is None


# --- pipeline ----------------------------------------------------------------


def pages(**overrides):
    data = {
        "acme.de/impressum": FakeResponse(text=IMPRESSUM, url="https://acme.de/impressum"),
        "acme.de": FakeResponse(text=HOME, url="https://acme.de/"),
    }
    data.update(overrides)
    return data


def test_pipeline_success():
    http = FakeHttp(pages())
    outcome = enrich.enrich_company(row(), HTTP, CONFIG, session=http, extractor=extractor(GOOD))
    assert outcome.status is Status.OK
    assert outcome.impressum_url == "https://acme.de/impressum"
    assert outcome.extraction["company_name"] == "ACME GmbH"
    assert "Angaben gemäß § 5 TMG" in optimized_text(outcome)


def optimized_text(outcome):
    return outcome.optimization.text if outcome.optimization else ""


def test_pipeline_sends_optimized_visible_text_not_html():
    http = FakeHttp(pages())
    extract = extractor(GOOD)
    enrich.enrich_company(row(), HTTP, CONFIG, session=http, extractor=extract)
    assert extract.last_text is not None
    assert "Angaben gemäß § 5 TMG" in extract.last_text
    assert "<p>" not in extract.last_text


def test_empty_web_marks_no_website_without_requests():
    http = FakeHttp({})
    outcome = enrich.enrich_company(row(web="  "), HTTP, CONFIG, session=http, extractor=extractor(GOOD))
    assert outcome.status is Status.NO_WEBSITE
    assert http.calls == []


def test_home_page_without_impressum_link():
    http = FakeHttp({"acme.de": FakeResponse(text="<a href='/about'>About</a>")})
    outcome = enrich.enrich_company(row(), HTTP, CONFIG, session=http, extractor=extractor(GOOD))
    assert outcome.status is Status.NO_IMPRESSUM_LINK_FOUND
    assert "acme.de" in outcome.reason


def test_http_error_on_home_page_is_error_status():
    http = FakeHttp({"acme.de": FakeResponse(status_code=503)})
    outcome = enrich.enrich_company(row(), HTTP, CONFIG, session=http, extractor=extractor(GOOD))
    assert outcome.status is Status.ERROR
    assert "503" in outcome.reason


def test_connection_error_is_error_status():
    http = FakeHttp({"acme.de": requests.ConnectionError("down")})
    outcome = enrich.enrich_company(row(), HTTP, CONFIG, session=http, extractor=extractor(GOOD))
    assert outcome.status is Status.ERROR


def test_deepseek_error_is_status_4_but_url_is_kept():
    http = FakeHttp(pages())
    outcome = enrich.enrich_company(
        row(), HTTP, CONFIG, session=http, extractor=extractor(DeepSeekError("boom"))
    )
    assert outcome.status is Status.ERROR
    assert outcome.impressum_url == "https://acme.de/impressum"
    assert "boom" in outcome.reason


def test_empty_extraction_is_parsing_failed():
    http = FakeHttp(pages())
    outcome = enrich.enrich_company(
        row(), HTTP, CONFIG, session=http, extractor=extractor(normalize_extraction({}))
    )
    assert outcome.status is Status.PARSING_FAILED


# --- batch processing / persistence -------------------------------------------


class RecordingCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class RecordingConnection(RecordingCursor):
    def cursor(self):
        return RecordingCursor()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def test_process_rows_writes_every_row_and_continues_after_errors(monkeypatch):
    written = []

    class Conn(RecordingConnection):
        def cursor(self):
            cursor = RecordingCursor()

            def execute(sql, params=None):
                written.append(params)

            cursor.execute = execute
            return cursor

    http = FakeHttp(pages())
    rows = [row(id=1), row(id=2, company_name="Broken", web="broken.de")]
    http.pages["broken.de/impressum"] = FakeResponse(status_code=404)

    counts = enrich.process_rows(
        rows, HTTP, CONFIG, lambda: Conn(), session=http, extractor=extractor(GOOD)
    )
    assert counts == {"OK": 1, "ERROR": 1}
    assert [params[-1] for params in written] == [1, 2]
    assert written[0][0] == 0
    assert written[1][0] == 4


def test_select_rows_default_excludes_ok_rows():
    cursor = RecordingCursor()
    enrich.select_rows(cursor, None, None, None)
    sql, params = cursor.executed[0]
    assert "status IS NULL OR status <> 0" in sql
    assert "LIMIT" not in sql
    assert params == ()


def test_select_rows_filters():
    cursor = RecordingCursor()
    enrich.select_rows(cursor, "ACME GmbH", 5, 1)
    sql, params = cursor.executed[0]
    assert "company_name = %s" in sql and "status = %s" in sql and "LIMIT %s" in sql
    assert params == ("ACME GmbH", 1, 5)
    assert "ORDER BY id" in sql


def test_cli_parsing():
    args = enrich.parse_args(["--company", "ACME", "--limit", "3", "--status", "1"])
    assert (args.company, args.limit, args.status) == ("ACME", 3, 1)


def test_cli_accepts_symbolic_status():
    assert enrich.parse_args(["--status", "ok"]).status == 0


@pytest.mark.parametrize("value", ["null", "pending", "9", "nope"])
def test_cli_rejects_bad_status(value):
    with pytest.raises(SystemExit):
        enrich.parse_args(["--status", value])


def test_main_without_api_key_returns_2(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert enrich.main([]) == 2


def test_select_rows_uses_none_filter_for_company(monkeypatch):
    cursor = RecordingCursor()
    enrich.select_rows(cursor, None, 2, None)
    sql, params = cursor.executed[0]
    assert params == (2,)
