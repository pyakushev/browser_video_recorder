"""zoom_worker.process_job tests with a fake subprocess/db boundary."""

import subprocess
from pathlib import Path

import zoom_worker
from zoom_worker import Settings


def make_job(tmp_path, attempts=1):
    video = tmp_path / "clip.webm"
    video.write_bytes(b"video")
    sidecar = tmp_path / "clip.webm.zoom.json"
    sidecar.write_text("{}", encoding="utf-8")
    return {
        "id": 7,
        "video_id": 11,
        "attempts": attempts,
        "video": {
            "id": 11,
            "company_name": "ACME GmbH",
            "file_path": str(video),
            "zoom_sidecar_path": str(sidecar),
            "processed_file_path": None,
        },
    }


def settings(**overrides):
    values = dict(
        public_base_url="http://localhost:8000",
        ffmpeg="ffmpeg",
        poll_sec=1.0,
        timeout_sec=30.0,
        max_attempts=3,
        stale_sec=1800.0,
    )
    values.update(overrides)
    return Settings(**values)


def install_finish(monkeypatch, result=True):
    calls = []

    def fake_finish(job, status, error=None, output=None, public_base_url="http://localhost:8000"):
        calls.append(
            {
                "job": job,
                "status": status,
                "error": error,
                "output": output,
                "public_base_url": public_base_url,
            }
        )
        return result

    monkeypatch.setattr(zoom_worker, "_finish_job", fake_finish)
    return calls


def test_success_marks_done_and_keeps_output(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    calls = install_finish(monkeypatch)

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--out") + 1])
        output.write_bytes(b"zoomed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(zoom_worker.subprocess, "run", fake_run)
    status = zoom_worker.process_job(job, settings())

    assert status == "done"
    assert calls[-1]["status"] == "done"
    assert calls[-1]["output"].name == "clip_zoomed.webm"
    assert calls[-1]["output"].is_file()


def test_missing_sidecar_is_skipped(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    Path(job["video"]["zoom_sidecar_path"]).unlink()
    calls = install_finish(monkeypatch)

    status = zoom_worker.process_job(job, settings())
    assert status == "skipped"
    assert calls[-1]["status"] == "skipped"
    assert "sidecar" in (calls[-1]["error"] or "")


def test_missing_video_fails(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    Path(job["video"]["file_path"]).unlink()
    calls = install_finish(monkeypatch)

    status = zoom_worker.process_job(job, settings())
    assert status == "failed"
    assert calls[-1]["status"] == "failed"


def test_ffmpeg_failure_is_reported(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    calls = install_finish(monkeypatch)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "boom from ffmpeg")

    monkeypatch.setattr(zoom_worker.subprocess, "run", fake_run)
    status = zoom_worker.process_job(job, settings())

    assert status == "failed"
    assert "boom from ffmpeg" in (calls[-1]["error"] or "")


def test_too_many_attempts_fails_without_ffmpeg(tmp_path, monkeypatch):
    job = make_job(tmp_path, attempts=4)
    calls = install_finish(monkeypatch)

    called = []
    monkeypatch.setattr(zoom_worker.subprocess, "run", lambda *a, **k: called.append(a))
    status = zoom_worker.process_job(job, settings(max_attempts=3))

    assert status == "failed"
    assert called == []
    assert "too many attempts" in (calls[-1]["error"] or "")


def test_superseded_job_removes_the_output(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    install_finish(monkeypatch, result=False)  # the job is no longer "running"

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--out") + 1])
        output.write_bytes(b"zoomed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(zoom_worker.subprocess, "run", fake_run)
    status = zoom_worker.process_job(job, settings())

    assert status == "cancelled"
    assert not (tmp_path / "clip_zoomed.webm").exists()
