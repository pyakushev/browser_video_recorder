"""deepseek_client.py: JSON parsing, normalization and the retry policy (mocked)."""

import json

import pytest
import requests

import deepseek_client as ds
from deepseek_client import (
    DeepSeekConfig,
    DeepSeekError,
    address_complete,
    extract_impressum,
    has_any_field,
    normalize_extraction,
    parse_json_object,
)

GOOD_JSON = {
    "company_name": "ACME GmbH",
    "postal_address": {
        "street": "Main Street",
        "house_no": "12",
        "postcode": "10115",
        "city": "Berlin",
        "country": "Germany",
    },
    "handelsregister": "HRB 12345",
    "vat_number": "DE123456789",
    "phone": "+49 30 1234567",
    "email": "Info@ACME.de",
    "responsible_person": "Max Muster",
}


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def chat_payload(content):
    return {"choices": [{"message": {"content": content}}]}


class FakeSession:
    """Records calls and replays a list of responses (or raises exceptions)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def config(key="test-key", retries=3):
    return DeepSeekConfig(api_key=key, retries=retries)


# --- JSON parsing ------------------------------------------------------------


def test_parse_plain_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    assert parse_json_object('```json\n{"a": 2}\n```') == {"a": 2}


def test_parse_json_embedded_in_prose():
    assert parse_json_object('Sure! {"a": 3} done') == {"a": 3}


@pytest.mark.parametrize("content", [None, "", "   ", "{broken", "just words", "[1, 2]"])
def test_parse_rejects_unusable_answers(content):
    assert parse_json_object(content) is None


# --- normalization -----------------------------------------------------------


def test_normalize_keeps_only_schema_keys_and_cleans_values():
    extracted = normalize_extraction({**GOOD_JSON, "extra": "ignored", "phone": " null "})
    assert "extra" not in extracted
    assert extracted["phone"] is None
    assert set(extracted["postal_address"]) == {
        "street",
        "house_no",
        "postcode",
        "city",
        "country",
    }


def test_normalize_missing_keys_become_none():
    extracted = normalize_extraction({})
    assert extracted["company_name"] is None
    assert all(value is None for value in extracted["postal_address"].values())
    assert not has_any_field(extracted)


def test_has_any_field_and_address_complete():
    extracted = normalize_extraction(GOOD_JSON)
    assert has_any_field(extracted)
    assert address_complete(extracted)

    partial = normalize_extraction({"company_name": "ACME"})
    assert has_any_field(partial)
    assert not address_complete(partial)


def test_address_complete_requires_all_four_parts_but_not_country():
    data = {**GOOD_JSON, "postal_address": {**GOOD_JSON["postal_address"], "house_no": None}}
    assert not address_complete(normalize_extraction(data))
    data["postal_address"] = {**GOOD_JSON["postal_address"], "country": None}
    assert address_complete(normalize_extraction(data))


# --- API call ----------------------------------------------------------------


def test_extract_sends_the_expected_request():
    session = FakeSession([FakeResponse(payload=chat_payload(json.dumps(GOOD_JSON)))])
    result = extract_impressum("Impressum text", config(), session=session, sleep=lambda _: None)

    call = session.calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    body = call["json"]
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["max_tokens"] >= 1024
    system = body["messages"][0]["content"]
    assert "json" in system.lower()
    assert '"company_name"' in system  # schema example
    assert "Impressum text" in body["messages"][1]["content"]
    assert result["email"] == "Info@ACME.de"
    assert result["postal_address"]["city"] == "Berlin"


def test_extract_uses_env_overrides():
    cfg = DeepSeekConfig(
        api_key="k", base_url="https://example.test/v1/", model="deepseek-flash"
    )
    session = FakeSession([FakeResponse(payload=chat_payload("{}"))])
    extract_impressum("t", cfg, session=session, sleep=lambda _: None)
    assert session.calls[0]["url"] == "https://example.test/v1/chat/completions"
    assert session.calls[0]["json"]["model"] == "deepseek-flash"


def test_blank_answer_is_retried():
    session = FakeSession(
        [
            FakeResponse(payload=chat_payload("   ")),
            FakeResponse(payload=chat_payload(json.dumps(GOOD_JSON))),
        ]
    )
    pauses = []
    result = extract_impressum("t", config(), session=session, sleep=pauses.append)
    assert len(session.calls) == 2
    assert result["company_name"] == "ACME GmbH"
    assert pauses  # backoff was applied


def test_server_error_is_retried_then_fails():
    session = FakeSession([FakeResponse(status=503, text="busy")] * 3)
    with pytest.raises(DeepSeekError, match="after 3 attempts"):
        extract_impressum("t", config(), session=session, sleep=lambda _: None)
    assert len(session.calls) == 3


def test_rate_limit_is_retried():
    session = FakeSession(
        [FakeResponse(status=429, text="slow down"),
         FakeResponse(payload=chat_payload(json.dumps(GOOD_JSON)))]
    )
    assert extract_impressum("t", config(), session=session, sleep=lambda _: None)


def test_auth_error_is_not_retried():
    session = FakeSession([FakeResponse(status=401, text="bad key")] * 3)
    with pytest.raises(DeepSeekError, match="401"):
        extract_impressum("t", config(), session=session, sleep=lambda _: None)
    assert len(session.calls) == 1


def test_network_error_is_retried():
    session = FakeSession(
        [requests.ConnectionError("down"),
         FakeResponse(payload=chat_payload(json.dumps(GOOD_JSON)))]
    )
    assert extract_impressum("t", config(), session=session, sleep=lambda _: None)


def test_missing_key_raises_before_any_request():
    session = FakeSession([])
    with pytest.raises(DeepSeekError, match="API_KEY"):
        extract_impressum("t", config(key=""), session=session, sleep=lambda _: None)
    assert session.calls == []


def test_unexpected_body_is_retried():
    session = FakeSession(
        [FakeResponse(payload={"unexpected": True}), FakeResponse(payload=chat_payload("{}"))]
    )
    assert extract_impressum("t", config(), session=session, sleep=lambda _: None) == normalize_extraction({})


# --- configuration / .env ----------------------------------------------------


def test_config_from_env_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\nDEEPSEEK_API_KEY='file-key'\nDEEPSEEK_MODEL=deepseek-flash\n", encoding="utf-8")
    cfg = ds.config_from_env(str(env))
    assert cfg.api_key == "file-key"
    assert cfg.model == "deepseek-flash"


def test_environment_wins_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shell-key")
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=file-key\n", encoding="utf-8")
    assert ds.config_from_env(str(env)).api_key == "shell-key"


def test_missing_env_file_is_ignored(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert ds.config_from_env("/does/not/exist").api_key == "k"
