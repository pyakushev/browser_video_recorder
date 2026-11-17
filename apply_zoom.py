#!/usr/bin/env python3
"""apply_zoom.py - render a Camtasia-style zoom into a recorded video.

Reads ``<video>.zoom.json`` (see ``zoom_sidecar.py``) and applies a
``zoompan`` filter: for every in/out pair the picture zooms in smoothly,
holds while the operator keeps the zoom on, and zooms out again.  The crop is
centred on the recorded mouse point and clamped to the frame.

Usage::

    python apply_zoom.py --video video/ACME_GmbH.webm
    python apply_zoom.py --video video/ACME_GmbH.webm --dry-run
    python apply_zoom.py --video ... --sidecar ... --out ... --ffmpeg /usr/bin/ffmpeg

The module keeps the timeline/filter maths free of ffmpeg so the tests can
check it without encoding anything.  Only ``main()`` and ``probe_video()``
touch the ffmpeg binaries.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from zoom_sidecar import (
    SidecarError,
    ZoomEvent,
    ZoomSidecar,
    load_sidecar,
    sidecar_path,
)

logger = logging.getLogger("apply_zoom")

DEFAULT_CRF = 32
DEFAULT_CPU_USED = 4


def smoothstep(t: float) -> float:
    """Classic ease-in-out curve: 0 -> 0, 1 -> 1, zero slope at both ends."""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def map_point(
    x: float,
    y: float,
    viewport: Tuple[float, float],
    video_size: Tuple[int, int],
) -> Tuple[float, float]:
    """Map sidecar (viewport) pixels to video pixels and clamp to the frame."""
    viewport_w, viewport_h = float(viewport[0]), float(viewport[1])
    video_w, video_h = float(video_size[0]), float(video_size[1])
    if viewport_w <= 0 or viewport_h <= 0:
        raise ValueError("viewport must be positive")
    if video_w <= 0 or video_h <= 0:
        raise ValueError("video size must be positive")
    px = x * video_w / viewport_w
    py = y * video_h / viewport_h
    return (
        min(max(px, 0.0), video_w),
        min(max(py, 0.0), video_h),
    )


@dataclass(frozen=True)
class Segment:
    """A half-open range of output frames with one zoom behaviour."""

    start: int          # first frame (inclusive)
    end: int            # last frame (exclusive)
    kind: str           # "in" | "hold" | "out"
    point: Tuple[float, float]
    factor: float
    ramp: int = 0       # frames used by an in/out ramp

    @property
    def frames(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Timeline:
    fps: float
    factor: float
    total_frames: int
    segments: Tuple[Segment, ...]

    def _segment_at(self, frame: int) -> Optional[Segment]:
        for segment in self.segments:
            if segment.start <= frame < segment.end:
                return segment
        return None

    def point_at(self, frame: int) -> Optional[Tuple[float, float]]:
        segment = self._segment_at(frame)
        return segment.point if segment is not None else None

    def zoom_at(self, frame: int) -> float:
        """Zoom factor for a 0-based output frame (1.0 = no zoom)."""
        if frame < 0 or frame >= self.total_frames:
            return 1.0
        segment = self._segment_at(frame)
        if segment is None:
            return 1.0
        if segment.kind == "hold" or segment.ramp <= 0:
            return self.factor
        progress = (frame - segment.start) / float(segment.ramp)
        eased = smoothstep(progress)
        if segment.kind == "in":
            return 1.0 + (self.factor - 1.0) * eased
        if segment.kind == "out":
            return 1.0 + (self.factor - 1.0) * (1.0 - eased)
        return 1.0

    def crop_at(
        self, frame: int, width: int, height: int
    ) -> Tuple[float, float, float]:
        """Return ``(crop_x, crop_y, zoom)`` for a frame, clamped to the frame."""
        zoom = self.zoom_at(frame)
        point = self.point_at(frame) or (width / 2.0, height / 2.0)
        crop_w = width / zoom
        crop_h = height / zoom
        crop_x = min(max(point[0] - crop_w / 2.0, 0.0), width - crop_w)
        crop_y = min(max(point[1] - crop_h / 2.0, 0.0), height - crop_h)
        return crop_x, crop_y, zoom

    def zoom_expression(self) -> str:
        """Piecewise ``z`` expression using ``on`` (0-based output frame)."""
        return _piecewise(self.segments, "1", self._zoom_value)

    def point_x_expression(self) -> str:
        return _piecewise(self.segments, "iw/2", lambda s: _num(s.point[0]))

    def point_y_expression(self) -> str:
        return _piecewise(self.segments, "ih/2", lambda s: _num(s.point[1]))

    def _zoom_value(self, segment: Segment) -> str:
        if segment.kind == "hold" or segment.ramp <= 0:
            return _num(segment.factor)
        ramp = float(segment.ramp)
        progress = f"(on-{segment.start})/{_num(ramp)}"
        eased = f"(3*pow({progress},2)-2*pow({progress},3))"
        if segment.kind == "in":
            return f"1+{_num(segment.factor - 1.0)}*{eased}"
        # "out": factor -> 1
        return f"1+{_num(segment.factor - 1.0)}*(1-{eased})"


def _num(value: float) -> str:
    """Compact decimal formatting for ffmpeg expressions."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _piecewise(
    segments: Sequence[Segment],
    default: str,
    value: Callable[[Segment], str],
) -> str:
    expression = default
    for segment in reversed(segments):
        if segment.end <= segment.start:
            continue
        expression = (
            f"if(between(on,{segment.start},{segment.end - 1}),"
            f"{value(segment)},{expression})"
        )
    return expression


def build_timeline(
    events: Sequence[ZoomEvent],
    duration: float,
    video_size: Tuple[int, int],
    viewport: Tuple[float, float],
    fps: float,
    zoom_factor: float,
    ramp_sec: float,
) -> Timeline:
    """Turn sidecar events into a frame-accurate zoom timeline."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"duration must be > 0, got {duration!r}")
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError(f"fps must be > 0, got {fps!r}")
    if not math.isfinite(zoom_factor) or zoom_factor <= 1.0:
        raise ValueError(f"zoom_factor must be > 1.0, got {zoom_factor!r}")
    if not math.isfinite(ramp_sec) or ramp_sec < 0:
        raise ValueError(f"ramp_sec must be >= 0, got {ramp_sec!r}")
    if len(video_size) != 2 or video_size[0] <= 0 or video_size[1] <= 0:
        raise ValueError(f"invalid video size {video_size!r}")

    total_frames = max(1, int(round(duration * fps)))

    def to_frame(t: float) -> int:
        frame = int(round(t * fps))
        return max(0, min(total_frames, frame))

    pairs: List[Tuple[int, int, ZoomEvent, bool]] = []
    open_event: Optional[ZoomEvent] = None
    open_frame = 0
    for event in sorted(events, key=lambda e: e.t):
        frame = to_frame(event.t)
        if event.type == "in":
            if open_event is not None:
                # A second "in" closes the previous zoom at this point.
                pairs.append((open_frame, frame, open_event, True))
            open_event = event
            open_frame = frame
        elif event.type == "out" and open_event is not None:
            pairs.append((open_frame, frame, open_event, True))
            open_event = None
    if open_event is not None:
        # Recording ended while zoomed: hold to the last frame.
        pairs.append((open_frame, total_frames, open_event, False))

    segments: List[Segment] = []
    for in_frame, out_frame, event, closed in pairs:
        if out_frame <= in_frame:
            continue
        span = out_frame - in_frame
        if ramp_sec <= 0:
            ramp = 0
        else:
            ramp = max(1, int(round(ramp_sec * fps)))
            ramp = min(ramp, span // 2)
        point = map_point(event.x, event.y, viewport, video_size)

        if ramp <= 0:
            segments.append(
                Segment(in_frame, out_frame, "hold", point, zoom_factor)
            )
            continue

        segments.append(Segment(in_frame, in_frame + ramp, "in", point, zoom_factor, ramp))
        hold_start = in_frame + ramp
        hold_end = out_frame - (ramp if closed else 0)
        if hold_end > hold_start:
            segments.append(Segment(hold_start, hold_end, "hold", point, zoom_factor))
        if closed:
            segments.append(
                Segment(out_frame - ramp, out_frame, "out", point, zoom_factor, ramp)
            )

    segments.sort(key=lambda s: s.start)
    return Timeline(fps=fps, factor=zoom_factor, total_frames=total_frames, segments=tuple(segments))


def build_filter(timeline: Timeline, width: int, height: int) -> str:
    """The complete ``-vf`` chain: normalise fps, zoompan, yuv420p."""
    zoom = timeline.zoom_expression()
    point_x = timeline.point_x_expression()
    point_y = timeline.point_y_expression()
    crop_x = f"max(0,min(({point_x})-iw/(2*zoom),iw-iw/zoom))"
    crop_y = f"max(0,min(({point_y})-ih/(2*zoom),ih-ih/zoom))"
    fps = _num(timeline.fps)
    size = f"{int(width)}x{int(height)}"
    return (
        f"fps={fps},"
        f"zoompan=z='{zoom}':x='{crop_x}':y='{crop_y}':d=1:s={size}:fps={fps},"
        f"format=yuv420p"
    )


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float


def _ffprobe_for(ffmpeg: str) -> str:
    ffmpeg_path = Path(ffmpeg)
    if ffmpeg_path.parent != Path(".") or ffmpeg_path.is_absolute():
        sibling = ffmpeg_path.with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def _parse_rate(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def probe_video(ffmpeg: str, video: Path) -> VideoInfo:
    """Read width/height/duration/fps with ffprobe (sibling of ``ffmpeg``)."""
    ffprobe = _ffprobe_for(ffmpeg)
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate:format=duration",
        "-of",
        "json",
        str(video),
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise ValueError(
            f"ffprobe not found ({ffprobe!r}); install ffmpeg or pass --ffmpeg"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise ValueError(f"ffprobe failed for {video}: {detail}") from exc
    except OSError as exc:
        raise ValueError(f"cannot run ffprobe ({ffprobe!r}): {exc}") from exc

    try:
        data = json.loads(completed.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError(f"cannot read video stream from {video}") from exc

    duration: Optional[float] = None
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"cannot read duration from {video}")

    fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
    if fps is None:
        raise ValueError(f"cannot read frame rate from {video}")
    return VideoInfo(width=width, height=height, duration=duration, fps=fps)


def build_command(
    ffmpeg: str,
    video: Path,
    output: Path,
    filter_expression: str,
    crf: int = DEFAULT_CRF,
    cpu_used: int = DEFAULT_CPU_USED,
) -> List[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vf",
        filter_expression,
        "-an",
        "-c:v",
        "libvpx-vp9",
        "-b:v",
        "0",
        "-crf",
        str(crf),
        "-row-mt",
        "1",
        "-cpu-used",
        str(cpu_used),
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]


def _default_output(video: Path) -> Path:
    # The renderer always writes VP9/WebM, regardless of the input container.
    return video.with_name(f"{video.stem}_zoomed.webm")


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return number


def _zoom_factor(value: str) -> float:
    number = _positive_float(value)
    if number <= 1.0:
        raise argparse.ArgumentTypeError("must be > 1.0")
    return number


def _non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}")
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return number


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a smooth Camtasia-style zoom to a recorded video."
    )
    parser.add_argument("--video", required=True, help="input .webm video")
    parser.add_argument(
        "--sidecar",
        default=None,
        help="zoom sidecar (default: <video>.zoom.json)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output video (default: <video>_zoomed.webm)",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.environ.get("FFMPEG_BIN") or "ffmpeg",
        help="ffmpeg binary (default: $FFMPEG_BIN or 'ffmpeg')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the ffmpeg command and exit without encoding",
    )
    parser.add_argument("--fps", type=_positive_float, default=None, help="override sidecar fps")
    parser.add_argument(
        "--zoom-factor", type=_zoom_factor, default=None, help="override sidecar zoom_factor"
    )
    parser.add_argument(
        "--ramp-sec", type=_non_negative_float, default=None, help="override sidecar ramp_sec"
    )
    parser.add_argument("--crf", type=int, default=DEFAULT_CRF, help="VP9 CRF (default: 32)")
    parser.add_argument(
        "--cpu-used", type=int, default=DEFAULT_CPU_USED, help="VP9 cpu-used (default: 4)"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    video = Path(args.video).expanduser()
    if not video.is_file():
        logger.error("input video not found: %s", video)
        return 2

    output = Path(args.out).expanduser() if args.out else _default_output(video)
    if output.resolve() == video.resolve():
        logger.error("refusing to overwrite the input video: %s", video)
        return 2

    sidecar_file = Path(args.sidecar).expanduser() if args.sidecar else sidecar_path(video)
    sidecar: Optional[ZoomSidecar] = None
    if sidecar_file.is_file():
        try:
            sidecar = load_sidecar(sidecar_file)
        except (OSError, SidecarError, UnicodeDecodeError) as exc:
            logger.error("cannot read sidecar %s: %s", sidecar_file, exc)
            return 2
    else:
        logger.warning("sidecar not found (%s): the video will be copied unchanged", sidecar_file)

    if sidecar is None or not sidecar.has_events:
        if args.dry_run:
            print(f"# no zoom events; would copy {video} -> {output}")
            return 0
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(video), str(output))
        except OSError as exc:
            logger.error("cannot copy %s -> %s: %s", video, output, exc)
            return 2
        logger.info("no zoom events: copied %s -> %s", video, output)
        return 0

    ffmpeg = args.ffmpeg
    try:
        info = probe_video(ffmpeg, video)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    fps = args.fps if args.fps is not None else sidecar.fps
    factor = args.zoom_factor if args.zoom_factor is not None else sidecar.zoom_factor
    ramp_sec = args.ramp_sec if args.ramp_sec is not None else sidecar.ramp_sec

    try:
        timeline = build_timeline(
            sidecar.events,
            duration=info.duration,
            video_size=(info.width, info.height),
            viewport=sidecar.viewport,
            fps=fps,
            zoom_factor=factor,
            ramp_sec=ramp_sec,
        )
    except ValueError as exc:
        logger.error("invalid zoom timeline: %s", exc)
        return 2

    if not timeline.segments:
        logger.warning("events produced no usable zoom segments; copying unchanged")
        if args.dry_run:
            print(f"# no usable zoom segments; would copy {video} -> {output}")
            return 0
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(video), str(output))
        return 0

    filter_expression = build_filter(timeline, info.width, info.height)
    logger.info(
        "video %sx%s %.2fs; %d event(s), %d segment(s), fps=%s, zoom=%s, ramp=%ss",
        info.width,
        info.height,
        info.duration,
        len(sidecar.events),
        len(timeline.segments),
        _num(fps),
        _num(factor),
        _num(ramp_sec),
    )

    if args.dry_run:
        command = build_command(
            ffmpeg, video, output, filter_expression, crf=args.crf, cpu_used=args.cpu_used
        )
        print(shlex.join(command))
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the real extension: ffmpeg picks the muxer from it.
    part = output.with_name(f"{output.stem}.part{output.suffix}")
    command = build_command(
        ffmpeg, video, part, filter_expression, crf=args.crf, cpu_used=args.cpu_used
    )
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        os.replace(str(part), str(output))
    except FileNotFoundError:
        part.unlink(missing_ok=True)
        logger.error("ffmpeg not found (%r); install ffmpeg or pass --ffmpeg", ffmpeg)
        return 3
    except subprocess.CalledProcessError as exc:
        part.unlink(missing_ok=True)
        detail = (exc.stderr or "").strip()
        logger.error("ffmpeg failed (exit %s): %s", exc.returncode, detail[-4000:])
        return 3
    except OSError as exc:
        part.unlink(missing_ok=True)
        logger.error("cannot write %s: %s", output, exc)
        return 3

    logger.info("wrote %s", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
