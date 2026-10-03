"""Filesystem helpers — atomic writes, magic byte detection."""
from __future__ import annotations

import os
from pathlib import Path

# Magic byte signatures for common container types
# Per https://en.wikipedia.org/wiki/List_of_file_signatures
MAGIC_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x00\x00\x00\x18ftyp", "mp4", "video/mp4"),         # MP4 family (offset 0)
    (b"\x00\x00\x00\x20ftyp", "mp4", "video/mp4"),
    (b"\x00\x00\x00\x1cftyp", "mp4", "video/mp4"),
    (b"\x00\x00\x00 ftyp", "mp4", "video/mp4"),
    (b"\x1aE\xdf\xa3", "webm", "video/webm"),               # WebM/Matroska
    (b"ID3", "mp3", "audio/mpeg"),                            # MP3 with ID3 tag
    (b"\xff\xfb", "mp3", "audio/mpeg"),                       # MP3 frame sync
    (b"\xff\xf3", "mp3", "audio/mpeg"),                       # MP3 v3
    (b"\xff\xf2", "mp3", "audio/mpeg"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
    (b"RIFF", "avi", "video/x-msvideo"),  # also WAV — check downstream
    (b"\x00\x00\x01\x00", "ico", "image/x-icon"),
    (b"OggS", "ogg", "audio/ogg"),
]


def detect_file_type(path: str | Path) -> tuple[str | None, str | None]:
    """Return (kind, mime_type) by reading first 32 bytes. None if unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None, None
    if not head:
        return None, None
    # Special: MP4 family — bytes 4-8 are 'ftyp'
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    for sig, kind, mime in MAGIC_SIGNATURES:
        if head.startswith(sig):
            return kind, mime
    # HTML error page detection
    stripped = head.lstrip()
    if stripped[:5].lower() in (b"<html", b"<!doc") or stripped[:9].lower() == b"<!doctype":
        return "html", "text/html"
    return None, None


def safe_unlink(path: str | Path) -> None:
    """Remove a file silently — never raise."""
    try:
        os.remove(path)
    except OSError:
        pass


def atomic_replace(part_path: str | Path, final_path: str | Path) -> bool:
    """os.replace part_path → final_path. Returns True on success."""
    try:
        os.replace(part_path, final_path)
        return True
    except OSError:
        return False


def ensure_dir(path: str | Path) -> None:
    """mkdir -p, never raises."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


def has_moov_atom(path: str | Path, max_search_bytes: int = 4 * 1024 * 1024) -> bool:
    """Probe for 'moov' atom in first 4 MB of an MP4 file.

    Truncated writes that died before the moov atom landed will fail this check.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(max_search_bytes)
        return b"moov" in data
    except OSError:
        return False
