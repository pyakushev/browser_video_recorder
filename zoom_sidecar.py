"""zoom_sidecar.py - the JSON sidecar shared by the recorder and the service.

A sidecar is written next to a recorded video as ``<video>.zoom.json``::

    {
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": [
            {"t": 3.0, "x": 640, "y": 360, "type": "in"},
            {"t": 8.0, "x": 640, "y": 360, "type": "out"}
        ]
    }

The module is deliberately free of ffmpeg, Playwright and database imports so
that the recorder, the web service and ``apply_zoom.py`` can all use it.

Backward compatibility
----------------------
Older sidecars may not contain ``type``.  In that case the events are read as
alternating ``in`` / ``out`` (the first one is an ``in``).  A bare JSON list of
events is also accepted.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

DEFAULT_VIEWPORT: Tuple[float, float] = (1280.0, 720.0)
DEFAULT_FPS = 25.0
DEFAULT_ZOOM_FACTOR = 2.0
DEFAULT_RAMP_SEC = 0.5

EVENT_IN = "in"
EVENT_OUT = "out"
EVENT_TYPES = (EVENT_IN, EVENT_OUT)

#: ``<video>.webm`` -> ``<video>.webm.zoom.json``
SIDECAR_SUFFIX = ".zoom.json"


class SidecarError(ValueError):
    """The sidecar is missing required data or is malformed."""


def _number(value: Any, what: str) -> float:
    """Convert to a finite float, rejecting bools and non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SidecarError(f"{what} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise SidecarError(f"{what} must be finite, got {value!r}")
    return number


def _positive(value: Any, what: str) -> float:
    number = _number(value, what)
    if number <= 0:
        raise SidecarError(f"{what} must be > 0, got {value!r}")
    return number


@dataclass(frozen=True)
class ZoomEvent:
    """One zoom trigger, in video pixels and seconds since the recording start."""

    t: float
    x: float
    y: float
    type: str = EVENT_IN

    def as_dict(self) -> Dict[str, Any]:
        return {"t": self.t, "x": self.x, "y": self.y, "type": self.type}


@dataclass(frozen=True)
class ZoomSidecar:
    """Parsed sidecar: recording metadata plus the ordered zoom events."""

    events: Tuple[ZoomEvent, ...] = ()
    viewport: Tuple[float, float] = DEFAULT_VIEWPORT
    fps: float = DEFAULT_FPS
    zoom_factor: float = DEFAULT_ZOOM_FACTOR
    ramp_sec: float = DEFAULT_RAMP_SEC

    @property
    def has_events(self) -> bool:
        return bool(self.events)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "fps": self.fps,
            "zoom_factor": self.zoom_factor,
            "ramp_sec": self.ramp_sec,
            "events": [event.as_dict() for event in self.events],
        }


def _parse_viewport(value: Any) -> Tuple[float, float]:
    if value is None:
        return DEFAULT_VIEWPORT
    if isinstance(value, Mapping):
        width = value.get("width")
        height = value.get("height")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        width, height = value
    else:
        raise SidecarError(f"viewport must be {{width, height}} or [width, height], got {value!r}")
    return _positive(width, "viewport.width"), _positive(height, "viewport.height")


def _parse_event(raw: Any, index: int, fallback_type: Optional[str]) -> ZoomEvent:
    if not isinstance(raw, Mapping):
        raise SidecarError(f"event #{index} must be an object, got {type(raw).__name__}")

    try:
        t = _number(raw["t"], f"event #{index}.t")
        x = _number(raw["x"], f"event #{index}.x")
        y = _number(raw["y"], f"event #{index}.y")
    except KeyError as exc:
        raise SidecarError(f"event #{index} is missing {exc.args[0]!r}") from exc

    kind = raw.get("type", fallback_type)
    if kind not in EVENT_TYPES:
        raise SidecarError(
            f"event #{index}.type must be one of {EVENT_TYPES}, got {kind!r}"
        )
    return ZoomEvent(t=max(0.0, t), x=x, y=y, type=str(kind))


def parse_sidecar(data: Any) -> ZoomSidecar:
    """Validate and normalize a sidecar loaded from JSON (dict or list)."""
    if isinstance(data, ZoomSidecar):
        return data
    if isinstance(data, list):
        data = {"events": data}
    if not isinstance(data, Mapping):
        raise SidecarError(f"sidecar must be a JSON object, got {type(data).__name__}")

    viewport = _parse_viewport(data.get("viewport"))
    fps = _positive(data.get("fps", DEFAULT_FPS), "fps")
    zoom_factor = _number(data.get("zoom_factor", DEFAULT_ZOOM_FACTOR), "zoom_factor")
    if zoom_factor <= 1.0:
        raise SidecarError(f"zoom_factor must be > 1.0, got {zoom_factor!r}")
    ramp_sec = _number(data.get("ramp_sec", DEFAULT_RAMP_SEC), "ramp_sec")
    if ramp_sec < 0:
        raise SidecarError(f"ramp_sec must be >= 0, got {ramp_sec!r}")

    events_raw = data.get("events", [])
    if events_raw is None:
        events_raw = []
    if not isinstance(events_raw, (list, tuple)):
        raise SidecarError(f"events must be a list, got {type(events_raw).__name__}")

    # Legacy sidecars have no "type" at all: alternate in/out by position.
    has_explicit_type = any(
        isinstance(event, Mapping) and "type" in event for event in events_raw
    )
    events: List[ZoomEvent] = []
    for index, raw in enumerate(events_raw):
        if has_explicit_type:
            fallback = None
            if isinstance(raw, Mapping) and "type" not in raw:
                # Mixed sidecar: infer the opposite of the previous event.
                fallback = EVENT_OUT if events and events[-1].type == EVENT_IN else EVENT_IN
        else:
            fallback = EVENT_IN if index % 2 == 0 else EVENT_OUT
        events.append(_parse_event(raw, index, fallback))

    events.sort(key=lambda event: event.t)
    return ZoomSidecar(
        events=tuple(events),
        viewport=viewport,
        fps=fps,
        zoom_factor=zoom_factor,
        ramp_sec=ramp_sec,
    )


def loads_sidecar(text: Union[str, bytes]) -> ZoomSidecar:
    """Parse sidecar JSON text/bytes (used by the upload endpoint)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SidecarError(f"invalid JSON: {exc}") from exc
    return parse_sidecar(data)


def load_sidecar(path: Union[str, os.PathLike]) -> ZoomSidecar:
    """Read and parse a sidecar file.  Raises OSError if the file is missing."""
    text = Path(path).read_text(encoding="utf-8")
    return loads_sidecar(text)


def dump_sidecar(sidecar: ZoomSidecar) -> str:
    return json.dumps(sidecar.as_dict(), ensure_ascii=False, indent=2) + "\n"


def write_sidecar(path: Union[str, os.PathLike], sidecar: ZoomSidecar) -> Path:
    """Atomically write a sidecar (a reader never sees a half-written file)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(dump_sidecar(sidecar))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp_path), str(target))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def sidecar_path(video_path: Union[str, os.PathLike]) -> Path:
    """The sidecar path for a video: ``/x/video.webm`` -> ``/x/video.webm.zoom.json``."""
    return Path(str(video_path) + SIDECAR_SUFFIX)
