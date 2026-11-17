"""Opt-in end-to-end test of the Docker stack (RUN_DOCKER_TESTS=1).

It builds an isolated compose project on port 18080, uploads a generated
video + sidecar, waits for the worker, and checks the processed URL in the
API and in PostgreSQL.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

if os.environ.get("RUN_DOCKER_TESTS") != "1":
    pytest.skip("set RUN_DOCKER_TESTS=1 to run Docker integration tests", allow_module_level=True)

requests = pytest.importorskip("requests")

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT = "zoomtest"
PORT = 18080
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PASSWORD = "zoomtest-password"
DB_NAME = "zoomtest"
DB_USER = "zoomtest_user"

DOCKER_CONFIG = tempfile.mkdtemp(prefix="dsh-docker-config-")
FFMPEG = shutil.which("ffmpeg")
if FFMPEG is None:  # pragma: no cover - environment dependent
    pytest.skip("ffmpeg is needed to generate the test video", allow_module_level=True)


def compose(*args, check=True, timeout=900):
    env = os.environ.copy()
    env.update(
        {
            "DB_PASSWORD": DB_PASSWORD,
            "DB_NAME": DB_NAME,
            "DB_USER": DB_USER,
            "WEB_PORT": str(PORT),
            "PUBLIC_BASE_URL": BASE_URL,
            # The sandbox home is read-only; give buildx a writable config dir.
            "DOCKER_CONFIG": DOCKER_CONFIG,
            "DOCKER_BUILDKIT": "0",
        }
    )
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, *args],
        cwd=str(REPO_ROOT),
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def make_video(path):
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=25:duration=2",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "40",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def wait_for_health(timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{BASE_URL}/health", timeout=2).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    pytest.fail("the web service did not become healthy in time")


def wait_for_job(job_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = requests.get(f"{BASE_URL}/jobs/{job_id}", timeout=10)
        response.raise_for_status()
        data = response.json()
        if data["status"] in ("done", "failed", "skipped", "cancelled"):
            return data
        time.sleep(1)
    pytest.fail(f"job {job_id} did not finish in time")


def test_docker_stack_processes_an_uploaded_video(tmp_path):
    video = tmp_path / "docker.webm"
    sidecar = tmp_path / "docker.webm.zoom.json"
    make_video(video)
    sidecar.write_text(
        json.dumps(
            {
                "viewport": {"width": 320, "height": 180},
                "fps": 25,
                "zoom_factor": 2.0,
                "ramp_sec": 0.5,
                "events": [
                    {"t": 0.3, "x": 80, "y": 45, "type": "in"},
                    {"t": 1.4, "x": 80, "y": 45, "type": "out"},
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        try:
            compose("up", "-d", "--build")
        except subprocess.CalledProcessError as exc:
            pytest.fail(
                "compose up failed:\nSTDOUT:\n%s\nSTDERR:\n%s"
                % (exc.stdout, exc.stderr)
            )
        wait_for_health()

        # ffmpeg/ffprobe must be present in the worker image
        ffmpeg_check = compose("exec", "-T", "worker", "ffmpeg", "-version")
        assert "ffmpeg version" in ffmpeg_check.stdout

        with open(video, "rb") as video_file, open(sidecar, "rb") as sidecar_file:
            response = requests.post(
                f"{BASE_URL}/upload",
                files={
                    "file": ("docker.webm", video_file, "video/webm"),
                    "zoom_sidecar": ("docker.webm.zoom.json", sidecar_file, "application/json"),
                },
                data={"company_name": "Docker Test"},
                timeout=120,
            )
        assert response.status_code == 200, response.text
        uploaded = response.json()
        assert uploaded["zoom_status"] == "pending"
        assert uploaded["job_id"] is not None

        job = wait_for_job(uploaded["job_id"])
        assert job["status"] == "done", job

        info = requests.get(
            f"{BASE_URL}/get_video_url", params={"company_name": "Docker Test"}, timeout=30
        ).json()
        assert info["zoom_status"] == "done"
        assert info["processed_video_url"]

        processed = requests.get(info["processed_video_url"], timeout=60)
        assert processed.status_code == 200
        assert len(processed.content) > 0

        # The URL is stored in PostgreSQL, not only in memory.
        query = (
            "SELECT processed_video_url IS NOT NULL, zoom_status "
            "FROM videos WHERE company_name = 'Docker Test'"
        )
        db_result = compose(
            "exec", "-T", "db", "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", query
        )
        assert db_result.stdout.strip() == "t|done"

        # A normal upload without a sidecar keeps the old behaviour.
        with open(video, "rb") as video_file:
            plain = requests.post(
                f"{BASE_URL}/upload",
                files={"file": ("plain.webm", video_file, "video/webm")},
                data={"company_name": "No Zoom"},
                timeout=120,
            )
        assert plain.status_code == 200, plain.text
        plain_data = plain.json()
        assert plain_data["zoom_status"] == "none"
        assert plain_data["job_id"] is None
        assert plain_data["processed_video_url"] is None
    finally:
        compose("down", "-v", check=False)
        shutil.rmtree(DOCKER_CONFIG, ignore_errors=True)
