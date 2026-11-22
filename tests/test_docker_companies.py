"""Opt-in end-to-end test of the company import + enrichment stack (Task 3).

Builds an isolated compose project on port 18081, imports a CSV via the web
service, then runs the enricher against fixture websites served from inside
``--network host`` containers (``ENRICH_MOCK=1``, no API key needed) and checks
statuses, the re-import guarantee and the token-optimization log.
"""

import json
import os
import shutil
import socket
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
PROJECT = "companiestest"
PORT = 18081
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PASSWORD = "companiestest-password"
DB_NAME = "companiestest"
DB_USER = "companiestest_user"
IMAGE = "playwright-zoom-service:local"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
DOCKER_CONFIG = tempfile.mkdtemp(prefix="dsh-docker-config-")


def compose(*args, check=True, timeout=900):
    env = os.environ.copy()
    env.update(
        {
            "DB_PASSWORD": DB_PASSWORD,
            "DB_NAME": DB_NAME,
            "DB_USER": DB_USER,
            "WEB_PORT": str(PORT),
            "PUBLIC_BASE_URL": BASE_URL,
            "ENRICH_MOCK": "1",
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


def wait_for_health(timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{BASE_URL}/health", timeout=2).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    pytest.fail("the web service did not become healthy in time")


def psql(query):
    result = compose("exec", "-T", "db", "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", query)
    return result.stdout.strip()


#: Runs inside a ``--network host`` container: serves the fixture pages on the
#: fixed port the test chose, so the compose network sees them via its gateway.
FIXTURE_SITE = r"""
import http.server, sys
sys.path.insert(0, "/fixture")
from pages import PAGES

port = int(sys.argv[1])

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

http.server.HTTPServer(("0.0.0.0", port), Handler).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    """A fixture website reachable from the enricher container.

    The compose network is created first so its gateway (the host address the
    bridge containers reach) is known, then a fixture server runs in a
    ``--network host`` container on the chosen port.  This works on any host,
    unlike ``host.docker.internal`` on older Docker engines.
    """
    try:
        compose("up", "-d", "--build")
        wait_for_health()
        try:
            network = json.loads(
                subprocess.run(
                    ["docker", "compose", "-p", PROJECT, "--profile", "tools",
                     "config", "--format", "json"],
                    cwd=str(REPO_ROOT),
                    env={**os.environ, "DB_PASSWORD": DB_PASSWORD,
                         "DOCKER_CONFIG": DOCKER_CONFIG},
                    check=True, capture_output=True, text=True,
                ).stdout
            )["networks"]["default"]["name"]
            gateway = subprocess.run(
                ["docker", "network", "inspect", network,
                 "--format", "{{(index .IPAM.Config 0).Gateway}}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except Exception:  # pragma: no cover - unusual compose setup
            gateway = "host.docker.internal"
    except subprocess.CalledProcessError as exc:
        pytest.fail("compose up failed:\n%s\n%s" % (exc.stdout, exc.stderr))

    port = _free_port()
    process = subprocess.Popen(
        ["docker", "run", "--rm", "--name", "companiestest-fixture", "--network", "host",
         "-i", "-v", f"{FIXTURE_DIR}:/fixture:ro", "--entrypoint", "python", IMAGE,
         "-c", FIXTURE_SITE, str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    probe = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        try:
            if requests.get(probe, timeout=2).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.3)
    else:  # pragma: no cover - environment dependent
        process.kill()
        pytest.fail("the fixture site did not start")
    try:
        yield f"http://{gateway}:{port}"
    finally:
        process.kill()
        process.wait(timeout=30)
        subprocess.run(["docker", "rm", "-f", "companiestest-fixture"],
                       capture_output=True, check=False)


def test_company_import_and_enrichment(site, tmp_path):
    csv_text = (
        "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web\n"
        f"https://duckduckgo.com;ACME GmbH;German;Germany;10115;Berlin;Main Street;12;{site}/\n"
        "https://duckduckgo.com;NoWeb Ltd;English;UK;;;;;;\n"
        f"https://duckduckgo.com;No Link Inc;English;UK;;;;;{site}/no-impressum\n"
    )
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    def upload():
        with open(csv_path, "rb") as handle:
            return requests.post(
                f"{BASE_URL}/upload_companies",
                files={"file": ("companies.csv", handle, "text/csv")},
                timeout=60,
            )

    try:
        # --- the HTML form exists ------------------------------------------
        form = requests.get(f"{BASE_URL}/upload_companies", timeout=30)
        assert form.status_code == 200
        assert 'enctype="multipart/form-data"' in form.text
        assert 'action="/upload_companies"' in form.text

        # --- import --------------------------------------------------------
        imported = upload()
        assert imported.status_code == 200, imported.text
        assert imported.json() == {"imported": 3, "updated": 0, "total_rows": 3}

        # the HTML form (redirect=1) renders the result instead of JSON
        with open(csv_path, "rb") as handle:
            rendered = requests.post(
                f"{BASE_URL}/upload_companies",
                params={"redirect": 1},
                files={"file": ("companies.csv", handle, "text/csv")},
                timeout=60,
            )
        assert rendered.status_code == 200
        assert "text/html" in rendered.headers.get("content-type", "")
        assert "Imported 0 new and updated 3" in rendered.text

        # a malformed CSV is rejected with 400
        bad = requests.post(
            f"{BASE_URL}/upload_companies",
            files={"file": ("bad.csv", b"URL;Web\nhttps://x.de;x.de\n", "text/csv")},
            timeout=30,
        )
        assert bad.status_code == 400 and "Company Name" in bad.json()["detail"]

        listed = requests.get(f"{BASE_URL}/companies", timeout=30).json()
        assert {row["company_name"] for row in listed} == {"ACME GmbH", "NoWeb Ltd", "No Link Inc"}
        assert all(row["status"] is None for row in listed)
        one = requests.get(f"{BASE_URL}/companies/ACME%20GmbH", timeout=30)
        assert one.status_code == 200 and one.json()["web"].startswith(site)
        assert requests.get(f"{BASE_URL}/companies/Unknown", timeout=30).status_code == 404

        # --- re-import is idempotent and keeps enrichment -------------------
        psql(
            "UPDATE companies SET status = 1, legal_name = 'KEEP ME', phone = 'KEEP PHONE' "
            "WHERE company_name = 'ACME GmbH'"
        )
        assert upload().json() == {"imported": 0, "updated": 3, "total_rows": 3}
        assert psql("SELECT legal_name FROM companies WHERE company_name = 'ACME GmbH'") == "KEEP ME"
        assert psql("SELECT count(*) FROM companies") == "3"

        # --- enrichment (mock mode: no API key needed) ---------------------
        run = compose("--profile", "tools", "run", "--rm", "-T", "enricher", check=False)
        output = run.stdout + run.stderr
        assert run.returncode == 0, output
        assert "chars" in output and "tokens ~" in output, output

        acme = psql(
            "SELECT status, legal_name, email, phone, impressum_url, "
            "raw_json IS NOT NULL FROM companies WHERE company_name = 'ACME GmbH'"
        ).split("|")
        assert acme[0] == "0", acme
        assert acme[2] == "info@acme-gmbh.de", acme
        assert acme[4].endswith("/de/impressum.html"), acme
        assert acme[5] == "t", acme
        assert psql("SELECT status FROM companies WHERE company_name = 'NoWeb Ltd'") == "5"
        assert (
            psql("SELECT status FROM companies WHERE company_name = 'No Link Inc'") == "1"
        )

        # OK rows are skipped on the next run (only non-OK rows are retried,
        # per §4: "processes not-yet-OK rows"); the API filter works.
        ok_rows = requests.get(f"{BASE_URL}/companies", params={"status": "ok"}, timeout=30).json()
        assert [row["company_name"] for row in ok_rows] == ["ACME GmbH"]
        rerun = compose("--profile", "tools", "run", "--rm", "-T", "enricher", check=False)
        rerun_output = rerun.stdout + rerun.stderr
        assert "ACME GmbH" not in rerun_output, rerun_output
        assert psql("SELECT status FROM companies WHERE company_name = 'ACME GmbH'") == "0"
    finally:
        compose("down", "-v", check=False)
        shutil.rmtree(DOCKER_CONFIG, ignore_errors=True)
