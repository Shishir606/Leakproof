"""Validate the structural requirements for the Day 4 backup demo recording."""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Box:
    kind: bytes
    payload_start: int
    end: int


def _boxes(data: bytes, start: int, end: int) -> list[Box]:
    boxes: list[Box] = []
    cursor = start
    while cursor + 8 <= end:
        size = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor + 4 : cursor + 8]
        header_size = 8
        if size == 1:
            if cursor + 16 > end:
                raise ValueError("truncated extended MP4 box")
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header_size = 16
        elif size == 0:
            size = end - cursor
        if size < header_size or cursor + size > end:
            raise ValueError("invalid MP4 box size")
        boxes.append(Box(kind, cursor + header_size, cursor + size))
        cursor += size
    if cursor != end:
        raise ValueError("trailing bytes outside an MP4 box")
    return boxes


def _child(data: bytes, parent: Box, kind: bytes) -> Box | None:
    return next(
        (item for item in _boxes(data, parent.payload_start, parent.end) if item.kind == kind),
        None,
    )


def _duration_seconds(data: bytes, movie: Box) -> float:
    header = _child(data, movie, b"mvhd")
    if header is None or header.payload_start + 20 > header.end:
        raise ValueError("MP4 movie header is missing or truncated")
    version = data[header.payload_start]
    if version == 0:
        timescale_offset = header.payload_start + 12
        duration_offset = header.payload_start + 16
        duration_size = 4
    elif version == 1:
        timescale_offset = header.payload_start + 20
        duration_offset = header.payload_start + 24
        duration_size = 8
    else:
        raise ValueError("unsupported MP4 movie-header version")
    if duration_offset + duration_size > header.end:
        raise ValueError("MP4 movie header is truncated")
    timescale = struct.unpack_from(">I", data, timescale_offset)[0]
    duration = int.from_bytes(
        data[duration_offset : duration_offset + duration_size], "big"
    )
    if timescale <= 0:
        raise ValueError("MP4 movie timescale must be positive")
    return duration / timescale


def _has_video_track(data: bytes, movie: Box) -> bool:
    for track in _boxes(data, movie.payload_start, movie.end):
        if track.kind != b"trak":
            continue
        media = _child(data, track, b"mdia")
        handler = _child(data, media, b"hdlr") if media is not None else None
        if handler is not None and handler.payload_start + 12 <= handler.end:
            if data[handler.payload_start + 8 : handler.payload_start + 12] == b"vide":
                return True
    return False


def validate_recording(
    path: Path,
    *,
    min_seconds: float = 75.0,
    max_seconds: float = 105.0,
    min_bytes: int = 100_000,
) -> float:
    if not path.is_file():
        raise ValueError(f"backup recording is missing: {path}")
    if path.suffix.casefold() != ".mp4":
        raise ValueError("backup recording must be an MP4 file")
    if path.stat().st_size < min_bytes:
        raise ValueError(f"backup recording is smaller than {min_bytes} bytes")
    data = path.read_bytes()
    top_level = _boxes(data, 0, len(data))
    if not any(item.kind == b"ftyp" for item in top_level):
        raise ValueError("backup recording is not a recognizable MP4 file")
    movie = next((item for item in top_level if item.kind == b"moov"), None)
    if movie is None:
        raise ValueError("backup recording does not contain an MP4 movie box")
    duration = _duration_seconds(data, movie)
    if not min_seconds <= duration <= max_seconds:
        raise ValueError(
            f"backup recording duration {duration:.1f}s is outside "
            f"{min_seconds:.1f}-{max_seconds:.1f}s"
        )
    if not _has_video_track(data, movie):
        raise ValueError("backup recording does not contain a video track")
    return duration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path("artifacts/demo/leakproof-90s-backup.mp4"),
    )
    parser.add_argument("--min-seconds", type=float, default=75.0)
    parser.add_argument("--max-seconds", type=float, default=105.0)
    parser.add_argument("--min-bytes", type=int, default=100_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        duration = validate_recording(
            args.path,
            min_seconds=args.min_seconds,
            max_seconds=args.max_seconds,
            min_bytes=args.min_bytes,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"validated backup demo recording: {args.path} "
        f"({duration:.1f}s, {args.path.stat().st_size} bytes)"
    )
    print("manual audio-muted and visually-hidden redaction reviews are still required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
