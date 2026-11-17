"""apply_zoom.py CLI paths that do not need a real ffmpeg encode."""

import json
from pathlib import Path

import pytest

import apply_zoom
from apply_zoom import VideoInfo, build_command, main


def write_video(path: Path, data: bytes = b"fake-video-bytes") -> Path:
    path.write_bytes(data)
    return path


def write_sidecar(path: Path, events):
    payload = {
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": events,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def fake_probe(monkeypatch):
    def probe(_ffmpeg, _video):
        return VideoInfo(width=1280, height=720, duration=10.0, fps=25.0)

    monkeypatch.setattr(apply_zoom, "probe_video", probe)
    return probe


def test_missing_video_returns_2(tmp_path):
    assert main(["--video", str(tmp_path / "missing.webm")]) == 2


def test_output_equal_to_input_is_rejected(tmp_path):
    video = write_video(tmp_path / "v.webm")
    assert main(["--video", str(video), "--out", str(video)]) == 2


def test_no_sidecar_copies_unchanged(tmp_path):
    video = write_video(tmp_path / "v.webm")
    output = tmp_path / "out.webm"
    assert main(["--video", str(video), "--out", str(output)]) == 0
    assert output.read_bytes() == video.read_bytes()


def test_empty_events_copy_unchanged(tmp_path, fake_probe):
    video = write_video(tmp_path / "v.webm")
    write_sidecar(tmp_path / "v.webm.zoom.json", [])
    output = tmp_path / "out.webm"
    assert main(["--video", str(video), "--out", str(output)]) == 0
    assert output.read_bytes() == video.read_bytes()


def test_no_events_dry_run_reports_copy_without_writing(tmp_path):
    video = write_video(tmp_path / "v.webm")
    output = tmp_path / "out.webm"
    assert main(["--video", str(video), "--out", str(output), "--dry-run"]) == 0
    assert not output.exists()


def test_invalid_sidecar_returns_2(tmp_path):
    video = write_video(tmp_path / "v.webm")
    (tmp_path / "v.webm.zoom.json").write_text("{not-json", encoding="utf-8")
    assert main(["--video", str(video)]) == 2


def test_dry_run_prints_a_complete_ffmpeg_command(tmp_path, fake_probe, capsys):
    video = write_video(tmp_path / "v.webm")
    write_sidecar(
        tmp_path / "v.webm.zoom.json",
        [
            {"t": 1.0, "x": 640, "y": 360, "type": "in"},
            {"t": 3.0, "x": 640, "y": 360, "type": "out"},
        ],
    )
    output = tmp_path / "zoomed.webm"
    assert main(["--video", str(video), "--out", str(output), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "zoompan" in printed
    assert "-c:v libvpx-vp9" in printed
    assert str(output) in printed
    assert not output.exists()


def test_dry_run_respects_custom_ffmpeg(tmp_path, fake_probe, capsys):
    video = write_video(tmp_path / "v.webm")
    write_sidecar(
        tmp_path / "v.webm.zoom.json",
        [{"t": 1.0, "x": 640, "y": 360, "type": "in"}],
    )
    assert main(
        ["--video", str(video), "--ffmpeg", "/opt/bin/ffmpeg", "--dry-run"]
    ) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("/opt/bin/ffmpeg ")


def test_legacy_sidecar_without_type_is_processed(tmp_path, fake_probe, capsys):
    video = write_video(tmp_path / "v.webm")
    write_sidecar(
        tmp_path / "v.webm.zoom.json",
        [
            {"t": 1.0, "x": 640, "y": 360},
            {"t": 3.0, "x": 640, "y": 360},
        ],
    )
    assert main(["--video", str(video), "--dry-run"]) == 0
    assert "between(on,25,36)" in capsys.readouterr().out


def test_default_output_name(tmp_path, fake_probe, capsys):
    video = write_video(tmp_path / "My Video.webm")
    write_sidecar(
        tmp_path / "My Video.webm.zoom.json",
        [{"t": 1.0, "x": 640, "y": 360, "type": "in"}],
    )
    assert main(["--video", str(video), "--dry-run"]) == 0
    assert "My Video_zoomed.webm" in capsys.readouterr().out


def test_build_command_places_arguments_in_the_right_slots(tmp_path):
    command = build_command(
        "/opt/ffmpeg",
        tmp_path / "in.webm",
        tmp_path / "out.webm",
        "fps=25,zoompan=z='1'",
        crf=30,
        cpu_used=6,
    )
    assert command[0] == "/opt/ffmpeg"
    assert command[command.index("-vf") + 1] == "fps=25,zoompan=z='1'"
    assert command[command.index("-crf") + 1] == "30"
    assert command[command.index("-cpu-used") + 1] == "6"
    assert command[-1].endswith("out.webm")


def test_default_output_is_always_webm():
    assert apply_zoom._default_output(Path("clip.mp4")).name == "clip_zoomed.webm"
    assert apply_zoom._default_output(Path("clip.webm")).name == "clip_zoomed.webm"


def test_parse_args_defaults():
    args = apply_zoom.parse_args(["--video", "x.webm"])
    assert args.ffmpeg
    assert args.crf == apply_zoom.DEFAULT_CRF
    assert args.cpu_used == apply_zoom.DEFAULT_CPU_USED
    assert args.dry_run is False
