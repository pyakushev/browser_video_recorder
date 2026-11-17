"""Coordinate maths, timeline and ffmpeg-filter generation."""

import math

import pytest

from apply_zoom import (
    Segment,
    Timeline,
    build_filter,
    build_timeline,
    map_point,
    smoothstep,
)
from zoom_sidecar import ZoomEvent


def event(t, x=640, y=360, kind="in"):
    return ZoomEvent(t=t, x=x, y=y, type=kind)


def timeline(events, duration=10.0, fps=25.0, factor=2.0, ramp=0.5, size=(1280, 720)):
    return build_timeline(
        events=events,
        duration=duration,
        video_size=size,
        viewport=(float(size[0]), float(size[1])),
        fps=fps,
        zoom_factor=factor,
        ramp_sec=ramp,
    )


def test_map_point_is_one_to_one_for_equal_sizes():
    assert map_point(640, 360, (1280, 720), (1280, 720)) == (640.0, 360.0)


def test_map_point_scales_viewport_to_video():
    assert map_point(640, 360, (1280, 720), (640, 360)) == (320.0, 180.0)


def test_map_point_clamps_to_frame():
    assert map_point(-10, 800, (1280, 720), (1280, 720)) == (0.0, 720.0)


def test_map_point_rejects_empty_viewport():
    with pytest.raises(ValueError):
        map_point(1, 2, (0, 720), (1280, 720))


def test_smoothstep_is_a_clean_ease_in_out():
    assert smoothstep(0.0) == 0.0
    assert smoothstep(0.5) == 0.5
    assert smoothstep(1.0) == 1.0
    values = [smoothstep(i / 20.0) for i in range(21)]
    assert values == sorted(values)
    assert all(0.0 <= value <= 1.0 for value in values)


def test_single_pair_has_ramp_hold_ramp():
    tl = timeline([event(1.0), event(3.0, kind="out")])
    assert tl.total_frames == 250
    assert [(s.kind, s.start, s.end) for s in tl.segments] == [
        ("in", 25, 37),
        ("hold", 37, 63),
        ("out", 63, 75),
    ]
    assert tl.zoom_at(24) == 1.0
    assert tl.zoom_at(25) == 1.0       # start of ramp-in
    assert tl.zoom_at(37) == 2.0       # hold starts
    assert tl.zoom_at(62) == 2.0       # hold ends
    assert tl.zoom_at(63) == 2.0       # start of ramp-out
    assert tl.zoom_at(75) == 1.0       # fully zoomed out again


def test_ramp_is_monotonic_and_endpoints_are_exact():
    tl = timeline([event(1.0), event(3.0, kind="out")])
    ramp_in = [tl.zoom_at(frame) for frame in range(25, 37)]
    ramp_out = [tl.zoom_at(frame) for frame in range(63, 75)]
    assert ramp_in == sorted(ramp_in)
    assert ramp_out == sorted(ramp_out, reverse=True)
    assert ramp_in[0] == 1.0
    assert ramp_in[-1] < 2.0
    assert ramp_out[0] == 2.0
    assert ramp_out[-1] > 1.0
    assert tl.zoom_at(37) == 2.0


def test_crop_centers_on_the_mouse_point_and_clamps():
    center = timeline([event(1.0, 640, 360), event(2.0, kind="out")])
    x, y, zoom = center.crop_at(37, 1280, 720)  # hold
    assert (x, y, zoom) == (320.0, 180.0, 2.0)

    top_left = timeline([event(1.0, 0, 0), event(2.0, kind="out")])
    assert top_left.crop_at(37, 1280, 720) == (0.0, 0.0, 2.0)

    bottom_right = timeline([event(1.0, 1280, 720), event(2.0, kind="out")])
    assert bottom_right.crop_at(37, 1280, 720) == (640.0, 360.0, 2.0)


def test_crop_never_leaves_the_frame_for_arbitrary_points():
    for px, py in [(0, 0), (1, 719), (1279, 1), (1279, 719), (640, 360)]:
        tl = timeline([event(1.0, px, py), event(2.0, kind="out")])
        for frame in range(tl.total_frames):
            x, y, zoom = tl.crop_at(frame, 1280, 720)
            assert 0.0 <= x <= 1280 - 1280 / zoom + 1e-9
            assert 0.0 <= y <= 720 - 720 / zoom + 1e-9


def test_open_ended_zoom_holds_to_the_end():
    tl = timeline([event(1.0)], duration=3.0)
    assert [s.kind for s in tl.segments] == ["in", "hold"]
    hold = tl.segments[-1]
    assert hold.end == tl.total_frames
    assert tl.zoom_at(tl.total_frames - 1) == 2.0
    assert tl.zoom_at(tl.total_frames) == 1.0


def test_multiple_pairs_are_independent():
    tl = timeline(
        [
            event(0.5, 100, 100, "in"),
            event(1.5, 100, 100, "out"),
            event(2.5, 900, 600, "in"),
            event(3.5, 900, 600, "out"),
        ],
        duration=4.0,
    )
    assert tl.zoom_at(50) == 1.0  # between the pairs
    assert tl.zoom_at(24) == 2.0  # first pair hold
    assert tl.zoom_at(80) > 1.0   # second pair ramp-out
    assert tl.crop_at(24, 1280, 720)[0] == 0.0  # point (100,100) clamps to 0
    second = tl.crop_at(74, 1280, 720)          # second pair hold
    assert second[0] == 900 - 1280 / 2 / 2
    assert second[1] == 720 - 720 / 2  # clamped at the bottom edge


def test_short_hold_shrinks_the_ramps_instead_of_overlapping():
    tl = timeline([event(1.0), event(1.2, kind="out")], duration=2.0)
    assert [(s.kind, s.start, s.end) for s in tl.segments] == [
        ("in", 25, 27),
        ("hold", 27, 28),
        ("out", 28, 30),
    ]
    previous_end = 0
    for segment in tl.segments:
        assert segment.start >= previous_end
        previous_end = segment.end
    assert math.isclose(tl.zoom_at(28), 2.0)


def test_zero_ramp_is_an_instant_zoom():
    tl = timeline([event(1.0), event(2.0, kind="out")], ramp=0.0)
    assert [s.kind for s in tl.segments] == ["hold"]
    assert tl.zoom_at(24) == 1.0
    assert tl.zoom_at(25) == 2.0
    assert tl.zoom_at(50) == 1.0


def test_out_without_in_is_ignored():
    tl = timeline([event(1.0, kind="out")])
    assert tl.segments == ()


def test_in_at_the_end_of_the_video_is_dropped():
    tl = timeline([event(10.0)], duration=10.0)
    assert tl.segments == ()


def test_second_in_closes_the_previous_zoom():
    tl = timeline([event(1.0), event(2.0), event(3.0, kind="out")], duration=4.0)
    # first in is closed at t=2, second in is closed at t=3
    assert len(tl.segments) >= 4


def test_build_filter_contains_zoompan_and_preserves_size():
    tl = timeline([event(1.0), event(3.0, kind="out")])
    expression = build_filter(tl, 1280, 720)
    assert expression.startswith("fps=25,")
    assert "zoompan=z='" in expression
    assert "d=1" in expression
    assert "s=1280x720" in expression
    assert "format=yuv420p" in expression
    assert "between(on,25,36)" in expression
    assert "iw-iw/zoom" in expression
    assert "ih-ih/zoom" in expression


def test_filter_generation_is_deterministic():
    tl = timeline([event(1.0), event(3.0, kind="out")])
    assert build_filter(tl, 1280, 720) == build_filter(tl, 1280, 720)


def test_timeline_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        timeline([event(1.0)], duration=0.0)
    with pytest.raises(ValueError):
        timeline([event(1.0)], fps=0.0)
    with pytest.raises(ValueError):
        timeline([event(1.0)], factor=1.0)
    with pytest.raises(ValueError):
        timeline([event(1.0)], ramp=-1.0)


def test_segment_frames_property():
    segment = Segment(10, 20, "hold", (1.0, 2.0), 2.0)
    assert segment.frames == 10
    assert isinstance(Timeline(25.0, 2.0, 100, (segment,)).segments[0], Segment)
