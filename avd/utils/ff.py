"""ffprobe / ffmpeg subprocess wrappers."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from typing import Any

_FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"
_FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"


def is_available() -> bool:
    """Return True if ffprobe is on PATH."""
    return shutil.which("ffprobe") is not None


async def probe(path: str | Path) -> dict[str, Any] | None:
    """Run `ffprobe -show_format -show_streams -print_format json <path>`.

    Returns parsed JSON dict, or None on failure.
    """
    cmd = [
        _FFPROBE_BIN,
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-print_format", "json",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        try:
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
    except (FileNotFoundError, OSError):
        return None


async def integrity_decode(path: str | Path) -> bool:
    """Run `ffmpeg -v error -i <path> -f null -` — exit 0 means stream is decodable.

    This catches truncated streams that ffprobe might parse but ffmpeg can't decode.
    """
    cmd = [
        _FFMPEG_BIN, "-v", "error",
        "-i", str(path),
        "-f", "null", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        rc = await proc.wait()
        return rc == 0
    except (FileNotFoundError, OSError):
        return False


def extract_probe_summary(probe_json: dict[str, Any]) -> dict[str, Any]:
    """Extract useful fields from ffprobe JSON."""
    out: dict[str, Any] = {
        "has_video_stream": False,
        "has_audio_stream": False,
        "duration_s": None,
        "codec_video": None,
        "codec_audio": None,
        "container": None,
    }
    fmt = probe_json.get("format") or {}
    streams = probe_json.get("streams") or []

    try:
        if fmt.get("duration"):
            out["duration_s"] = float(fmt["duration"])
    except (TypeError, ValueError):
        pass

    fmt_name = fmt.get("format_name") or fmt.get("format_long_name")
    out["container"] = fmt_name

    for s in streams:
        codec_type = s.get("codec_type")
        codec_name = s.get("codec_name")
        if codec_type == "video" and not out["has_video_stream"]:
            out["has_video_stream"] = True
            out["codec_video"] = codec_name
        elif codec_type == "audio" and not out["has_audio_stream"]:
            out["has_audio_stream"] = True
            out["codec_audio"] = codec_name

    return out
