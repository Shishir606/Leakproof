from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_validator() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "validate_demo_recording.py"
    spec = importlib.util.spec_from_file_location("validate_demo_recording", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate_recording = _load_validator().validate_recording


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _recording(path: Path, *, seconds: int = 90, handler: bytes = b"vide") -> Path:
    movie_header = bytes(12) + struct.pack(">II", 1_000, seconds * 1_000)
    track_handler = bytes(8) + handler + bytes(4)
    movie = _box(
        b"moov",
        _box(b"mvhd", movie_header)
        + _box(b"trak", _box(b"mdia", _box(b"hdlr", track_handler))),
    )
    payload = _box(b"ftyp", b"isom" + bytes(4)) + movie
    payload += _box(b"mdat", bytes(100_000))
    path.write_bytes(payload)
    return path


def test_valid_demo_recording_has_expected_duration_and_video(tmp_path: Path):
    path = _recording(tmp_path / "backup.mp4")

    assert validate_recording(path) == 90.0


def test_demo_recording_must_exist(tmp_path: Path):
    with pytest.raises(ValueError, match="missing"):
        validate_recording(tmp_path / "missing.mp4")


@pytest.mark.parametrize(
    ("seconds", "handler", "message"),
    [(30, b"vide", "duration"), (90, b"soun", "video track")],
)
def test_demo_recording_rejects_wrong_duration_or_missing_video(
    tmp_path: Path, seconds: int, handler: bytes, message: str
):
    path = _recording(tmp_path / "invalid.mp4", seconds=seconds, handler=handler)

    with pytest.raises(ValueError, match=message):
        validate_recording(path)
