"""video_client.py - client functions for the video web service (web_service.py).

`requests` is imported lazily inside the functions, so the recorder runs
without it when --server is not used.
"""

from __future__ import annotations

import mimetypes
import os

CONNECT_TIMEOUT = 10  # seconds
READ_TIMEOUT = 300  # uploading a large video can take a while


class VideoServiceError(RuntimeError):
    """The server answered with an error (or with something unexpected)."""


class VideoNotFoundError(VideoServiceError):
    """GetVideoURL: the server has no video for this company name (HTTP 404)."""


def _base_url(server_url: str) -> str:
    return server_url.strip().rstrip("/")


def _error_detail(response) -> str:  # type: ignore[no-untyped-def]
    try:
        return str(response.json().get("detail", response.text[:200]))
    except (ValueError, AttributeError):
        return response.text[:200]


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    import requests  # lazy import

    filename = os.path.basename(video_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(video_path, "rb") as fileobj:
        response = requests.post(
            f"{_base_url(server_url)}/upload",
            files={"file": (filename, fileobj, content_type)},
            data={"company_name": company_name},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    if not response.ok:
        raise VideoServiceError(
            f"upload failed: HTTP {response.status_code}: {_error_detail(response)}"
        )
    try:
        return str(response.json()["video_url"])
    except (ValueError, KeyError) as exc:
        raise VideoServiceError(f"unexpected upload response: {response.text[:200]!r}") from exc


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    import requests  # lazy import

    response = requests.get(
        f"{_base_url(server_url)}/get_video_url",
        params={"company_name": company_name},
        timeout=(CONNECT_TIMEOUT, 30),
    )
    if response.status_code == 404:
        raise VideoNotFoundError(f"no video for company {company_name!r}")
    if not response.ok:
        raise VideoServiceError(
            f"lookup failed: HTTP {response.status_code}: {_error_detail(response)}"
        )
    try:
        return str(response.json()["video_url"])
    except (ValueError, KeyError) as exc:
        raise VideoServiceError(f"unexpected lookup response: {response.text[:200]!r}") from exc
