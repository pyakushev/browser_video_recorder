"""deepseek_client.py - DeepSeek structured extraction of Impressum data.

The DeepSeek API is OpenAI-compatible: ``POST /chat/completions`` with
``response_format={"type": "json_object"}`` returns a JSON object.  The key is
read from ``DEEPSEEK_API_KEY`` (a ``.env`` file is loaded when present) and is
never logged or committed.

Everything here is injectable (``session`` and ``sleep``) so the tests can
run without a key and without network access.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger("deepseek")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

SYSTEM_PROMPT = """\
You extract legally required company information ("Impressum" / "Imprint") from
German-language website text.


Return ONLY a valid JSON object (no markdown fences, no commentary) that parses
with json.loads. The JSON must have exactly these keys:


{
    "company_name": "<string or null>",
    "postal_address": {
         "street": "<string or null>",
         "house_no": "<string or null>",
         "postcode": "<string or null>",
         "city": "<string or null>",
         "country": "<string or null>"
    },
    "handelsregister": "<string or null>",
    "vat_number": "<string or null>",
    "phone": "<string or null>",
    "email": "<string or null>",
    "responsible_person": "<string or null>"
}


Rules:
- Extract ONLY information actually present in the text; use null when absent.
- Do not invent or guess values.
- phone: keep the international format when possible (e.g. +49 ...).
- vat_number: German VAT ids look like DE followed by 9 digits.
- email: lowercase.
- responsible_person: the named person or role legally responsible for the
    content (e.g. Geschaeftsfuehrer, Vorstand, "inhaltlich verantwortlich").
- Output valid JSON only.
"""

USER_PROMPT_TEMPLATE = (
    "Extract the Impressum data from the text below and return JSON:\n\n{text}"
)

JSON_MODE_HINT = """\
Return a single JSON object. Schema example:
{"company_name": null, "postal_address": {"street": null, "house_no": null,
"postcode": null, "city": null, "country": null}, "handelsregister": null,
"vat_number": null, "phone": null, "email": null, "responsible_person": null}
"""

FULL_SYSTEM_PROMPT = SYSTEM_PROMPT + "\n" + JSON_MODE_HINT


EXTRACTED_KEYS = (
    "company_name",
    "postal_address",
    "handelsregister",
    "vat_number",
    "phone",
    "email",
    "responsible_person",
)
ADDRESS_KEYS = ("street", "house_no", "postcode", "city", "country")
REQUIRED_ADDRESS_KEYS = ("street", "house_no", "postcode", "city")


class DeepSeekError(RuntimeError):
    """The API could not be reached or did not return a usable JSON object."""


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES


def load_env_file(path: "Path | str" = ".env") -> None:
    """Minimal ``.env`` loader (KEY=VALUE lines) - no python-dotenv needed.

    Existing environment variables win, so a real key can always be exported.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def config_from_env(env_path: Optional[str] = ".env") -> DeepSeekConfig:
    """Build the config from the environment (optionally after reading .env)."""
    if env_path:
        load_env_file(env_path)
    return DeepSeekConfig(
        api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        max_tokens=int(os.environ.get("DEEPSEEK_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
        timeout=int(os.environ.get("DEEPSEEK_TIMEOUT", DEFAULT_TIMEOUT)),
        retries=max(1, int(os.environ.get("DEEPSEEK_RETRIES", DEFAULT_RETRIES))),
    )


def parse_json_object(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the model answer, tolerating markdown fences and prose around it.

    Returns None for empty/blank/unparseable answers (the API blanks out JSON
    that ran into ``max_tokens``).
    """
    if not content or not content.strip():
        return None
    text = content.strip()
    if text.startswith("```"):
        # Strip a fenced block: ```json ... ```
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _clean_value(value: Any) -> Any:
    """Trim strings, turn ``"null"``/blank into None, recurse into dicts."""
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        return None if text.lower() in ("", "null", "none", "n/a", "-") else text
    return value


def normalize_extraction(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep exactly the schema keys; missing keys become None."""
    address_raw = data.get("postal_address")
    address: Dict[str, Any] = {}
    if isinstance(address_raw, dict):
        address = {key: _clean_value(address_raw.get(key)) for key in ADDRESS_KEYS}
    elif isinstance(address_raw, str) and address_raw.strip():
        # Some answers flatten the address into one string.
        address = {"street": None, "house_no": None, "postcode": None, "city": None,
                   "country": _clean_value(address_raw)}
    else:
        address = {key: None for key in ADDRESS_KEYS}
    result: Dict[str, Any] = {
        "company_name": _clean_value(data.get("company_name")),
        "postal_address": address,
    }
    for key in EXTRACTED_KEYS:
        if key not in ("company_name", "postal_address"):
            result[key] = _clean_value(data.get(key))
    return result


def has_any_field(extracted: Dict[str, Any]) -> bool:
    """True when at least one field was detected (any value in the nested dict)."""
    if extracted.get("company_name"):
        return True
    if any(extracted.get("postal_address", {}).get(key) for key in ADDRESS_KEYS):
        return True
    return any(extracted.get(key) for key in EXTRACTED_KEYS if key != "postal_address")


def address_complete(extracted: Dict[str, Any]) -> bool:
    """All of postcode/city/street/house_no are non-empty (country is optional)."""
    address = extracted.get("postal_address") or {}
    return all(address.get(key) for key in REQUIRED_ADDRESS_KEYS)


def extract_impressum(
    text: str,
    config: DeepSeekConfig,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Call DeepSeek and return the normalized JSON object.

    Retries blank/unparseable answers and 5xx/429 responses with backoff; a
    permanent API error (invalid key, 4xx) raises :class:`DeepSeekError`
    immediately.
    """
    if not config.api_key:
        raise DeepSeekError("DEEPSEEK_API_KEY is not set")
    http = session or requests.Session()
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": FULL_SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(text=text)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": config.max_tokens,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    last_error = "no attempt was made"
    for attempt in range(config.retries):
        try:
            response = http.post(url, headers=headers, json=payload, timeout=config.timeout)
        except requests.RequestException as exc:
            last_error = f"request failed: {exc}"
        else:
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            elif response.status_code >= 400:
                raise DeepSeekError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            else:
                try:
                    content = response.json()["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    last_error = f"unexpected response body: {exc}"
                else:
                    parsed = parse_json_object(content)
                    if parsed is not None:
                        return normalize_extraction(parsed)
                    last_error = "empty or non-JSON answer"

        logger.warning(
            "DeepSeek attempt %d/%d failed: %s", attempt, config.retries, last_error
        )
        if attempt < config.retries:
            sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise DeepSeekError(f"DeepSeek failed after {config.retries} attempts: {last_error}")
