"""Sidecar parsing / writing tests (no ffmpeg, no Playwright)."""

import json

import pytest

from zoom_sidecar import (
    DEFAULT_FPS,
    DEFAULT_RAMP_SEC,
    DEFAULT_VIEWPORT,
    DEFAULT_ZOOM_FACTOR,
    SidecarError,
    ZoomEvent,
    ZoomSidecar,
    dump_sidecar,
    load_sidecar,
    loads_sidecar,
    parse_sidecar,
    sidecar_path,
    write_sidecar,
)


def _payload(**overrides):
    data = {
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": [
            {"t": 3.0, "x": 640, "y": 360, "type": "in"},
            {"t": 8.0, "x": 640, "y": 360, "type": "out"},
        ],
    }
    data.update(overrides)
    return data


def test_parse_valid_sidecar():
    sidecar = parse_sidecar(_payload())
    assert sidecar.viewport == (1280.0, 720.0)
    assert sidecar.fps == 25.0
    assert sidecar.zoom_factor == 2.0
    assert sidecar.ramp_sec == 0.5
    assert [event.type for event in sidecar.events] == ["in", "out"]
    assert sidecar.has_events


def test_events_are_sorted_by_time():
    sidecar = parse_sidecar(
        _payload(
            events=[
                {"t": 8.0, "x": 1, "y": 2, "type": "out"},
                {"t": 3.0, "x": 3, "y": 4, "type": "in"},
            ]
        )
    )
    assert [event.t for event in sidecar.events] == [3.0, 8.0]


def test_legacy_sidecar_without_type_alternates_in_out():
    sidecar = parse_sidecar(
        _payload(
            events=[
                {"t": 1.0, "x": 10, "y": 20},
                {"t": 2.0, "x": 10, "y": 20},
                {"t": 3.0, "x": 10, "y": 20},
                {"t": 4.0, "x": 10, "y": 20},
            ]
        )
    )
    assert [event.type for event in sidecar.events] == ["in", "out", "in", "out"]


def test_legacy_odd_count_keeps_open_ended_last_event():
    sidecar = parse_sidecar(
        _payload(
            events=[
                {"t": 1.0, "x": 10, "y": 20},
                {"t": 2.0, "x": 10, "y": 20},
                {"t": 3.0, "x": 10, "y": 20},
            ]
        )
    )
    assert [event.type for event in sidecar.events] == ["in", "out", "in"]


def test_mixed_sidecar_infers_missing_type():
    sidecar = parse_sidecar(
        _payload(
            events=[
                {"t": 1.0, "x": 10, "y": 20},
                {"t": 2.0, "x": 10, "y": 20, "type": "out"},
                {"t": 3.0, "x": 10, "y": 20},
            ]
        )
    )
    assert [event.type for event in sidecar.events] == ["in", "out", "in"]


def test_bare_list_is_accepted():
    sidecar = parse_sidecar(
        [{"t": 1.0, "x": 10, "y": 20, "type": "in"}]
    )
    assert sidecar.has_events
    assert sidecar.viewport == DEFAULT_VIEWPORT


def test_defaults_for_missing_metadata():
    sidecar = parse_sidecar({"events": []})
    assert sidecar.fps == DEFAULT_FPS
    assert sidecar.zoom_factor == DEFAULT_ZOOM_FACTOR
    assert sidecar.ramp_sec == DEFAULT_RAMP_SEC
    assert not sidecar.has_events


def test_negative_time_is_clamped_to_zero():
    sidecar = parse_sidecar(
        _payload(events=[{"t": -1.0, "x": 10, "y": 20, "type": "in"}])
    )
    assert sidecar.events[0].t == 0.0


def test_unknown_event_type_is_rejected():
    with pytest.raises(SidecarError, match="type"):
        parse_sidecar(_payload(events=[{"t": 1.0, "x": 10, "y": 20, "type": "boom"}]))


def test_event_must_be_an_object():
    with pytest.raises(SidecarError, match="object"):
        parse_sidecar(_payload(events=["not-an-event"]))


def test_event_requires_coordinates():
    with pytest.raises(SidecarError, match="x"):
        parse_sidecar(_payload(events=[{"t": 1.0, "y": 20, "type": "in"}]))


def test_events_must_be_a_list():
    with pytest.raises(SidecarError, match="events must be a list"):
        parse_sidecar(_payload(events={"t": 1.0}))


def test_non_finite_number_is_rejected():
    with pytest.raises(SidecarError, match="finite"):
        parse_sidecar(_payload(events=[{"t": float("nan"), "x": 1, "y": 2, "type": "in"}]))


def test_zoom_factor_must_be_greater_than_one():
    with pytest.raises(SidecarError, match="zoom_factor"):
        parse_sidecar(_payload(zoom_factor=1.0))


def test_negative_ramp_is_rejected():
    with pytest.raises(SidecarError, match="ramp_sec"):
        parse_sidecar(_payload(ramp_sec=-0.1))


def test_invalid_json_is_reported():
    with pytest.raises(SidecarError, match="invalid JSON"):
        loads_sidecar("{not json")


def test_dump_and_load_roundtrip(tmp_path):
    sidecar = ZoomSidecar(
        events=(ZoomEvent(1.0, 10.0, 20.0, "in"),),
        viewport=(800.0, 600.0),
        fps=30.0,
        zoom_factor=1.5,
        ramp_sec=0.25,
    )
    path = tmp_path / "video.webm.zoom.json"
    write_sidecar(path, sidecar)
    loaded = load_sidecar(path)
    assert loaded == sidecar
    assert json.loads(dump_sidecar(sidecar))["events"][0]["type"] == "in"


def test_sidecar_path_names_the_file_next_to_the_video():
    assert str(sidecar_path("/videos/ACME GmbH.webm")) == "/videos/ACME GmbH.webm.zoom.json"


def test_write_sidecar_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "v.zoom.json"
    write_sidecar(path, ZoomSidecar(events=(ZoomEvent(1.0, 2.0, 3.0),)))
    assert path.is_file()
    leftovers = [p for p in tmp_path.iterdir() if p.name != path.name]
    assert leftovers == []
