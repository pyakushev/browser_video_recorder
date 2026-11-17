"""Real ffmpeg encode: skipped automatically when ffmpeg is not installed."""

import json
import shutil
import subprocess

import pytest

import apply_zoom

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

if FFMPEG is None or FFPROBE is None:  # pragma: no cover - environment dependent
    pytest.skip("ffmpeg/ffprobe are not installed", allow_module_level=True)


def make_video(path, duration=2.0, size="320x180", rate=25):
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
            f"testsrc=size={size}:rate={rate}:duration={duration}",
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


def test_apply_zoom_encodes_and_preserves_resolution(tmp_path):
    video = tmp_path / "clip.webm"
    make_video(video)
    sidecar = tmp_path / "clip.webm.zoom.json"
    sidecar.write_text(
        json.dumps(
            {
                "viewport": {"width": 320, "height": 180},
                "fps": 25,
                "zoom_factor": 2.0,
                "ramp_sec": 0.5,
                "events": [
                    {"t": 0.3, "x": 80, "y": 45, "type": "in"},
                    {"t": 1.5, "x": 80, "y": 45, "type": "out"},
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "clip_zoomed.webm"

    assert apply_zoom.main(["--video", str(video), "--out", str(output)]) == 0
    assert output.is_file() and output.stat().st_size > 0

    info = apply_zoom.probe_video(FFMPEG, output)
    assert (info.width, info.height) == (320, 180)
    assert info.duration == pytest.approx(2.0, abs=0.1)


def test_dry_run_does_not_create_the_output(tmp_path):
    video = tmp_path / "clip.webm"
    make_video(video, duration=1.0)
    (tmp_path / "clip.webm.zoom.json").write_text(
        json.dumps(
            {
                "viewport": {"width": 320, "height": 180},
                "fps": 25,
                "zoom_factor": 2.0,
                "ramp_sec": 0.5,
                "events": [{"t": 0.2, "x": 10, "y": 10, "type": "in"}],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "clip_dry.webm"
    assert apply_zoom.main(["--video", str(video), "--out", str(output), "--dry-run"]) == 0
    assert not output.exists()


def test_open_ended_zoom_encodes_to_the_end(tmp_path):
    video = tmp_path / "open.webm"
    make_video(video, duration=1.0)
    (tmp_path / "open.webm.zoom.json").write_text(
        json.dumps(
            {
                "viewport": {"width": 320, "height": 180},
                "fps": 25,
                "zoom_factor": 2.0,
                "ramp_sec": 0.2,
                "events": [{"t": 0.1, "x": 160, "y": 90, "type": "in"}],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "open_zoomed.webm"
    assert apply_zoom.main(["--video", str(video), "--out", str(output)]) == 0
    info = apply_zoom.probe_video(FFMPEG, output)
    assert (info.width, info.height) == (320, 180)
    assert info.duration == pytest.approx(1.0, abs=0.1)
