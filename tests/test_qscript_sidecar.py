"""qscript sidecar lifecycle: written only with events, stale file removed."""

from pathlib import Path

import qscript
from zoom_sidecar import load_sidecar


def test_no_events_does_not_write_and_removes_a_stale_sidecar(tmp_path):
    video = tmp_path / "clip.webm"
    video.write_bytes(b"video")
    stale = Path(str(video) + ".zoom.json")
    stale.write_text("{}", encoding="utf-8")

    qscript._reset_zoom_state()
    try:
        assert qscript._write_zoom_sidecar(video) is None
        assert not stale.exists()
    finally:
        qscript._reset_zoom_state()


def test_events_create_the_sidecar(tmp_path):
    video = tmp_path / "clip.webm"
    video.write_bytes(b"video")

    qscript._reset_zoom_state()
    try:
        qscript._zoom_events[:] = [
            {"t": 1.25, "x": 100.0, "y": 200.0, "type": "in"},
            {"t": 2.5, "x": 100.0, "y": 200.0, "type": "out"},
        ]
        path = qscript._write_zoom_sidecar(video)
        assert path is not None
        sidecar = load_sidecar(path)
        assert [event.type for event in sidecar.events] == ["in", "out"]
        assert sidecar.fps == 25.0
        assert sidecar.zoom_factor == 2.0
        assert qscript.zoom_sidecar_path(video) == str(path)
    finally:
        qscript._reset_zoom_state()
