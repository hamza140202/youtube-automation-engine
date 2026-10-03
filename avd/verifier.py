"""Verifier agent — 6-layer file integrity check."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from avd.config import get_settings
from avd.models import VerifierReport
from avd.utils.ff import extract_probe_summary, integrity_decode, probe
from avd.utils.fs import detect_file_type, has_moov_atom
from avd.utils.logging import get_logger

log = get_logger("avd.verifier")


class Verifier:
    """6-layer integrity check agent."""

    __version__ = "1.0.0"

    def __init__(self) -> None:
        self.settings = get_settings()

    async def verify(self, artifact: Path, *, expected_meta: dict | None = None) -> VerifierReport:
        """Run all 6 layers. Returns VerifierReport. Never raises."""
        report = VerifierReport(artifact_path=Path(artifact), issues=[])
        try:
            # Layer 1: existence
            if not Path(artifact).exists():
                report.issues.append("E_FILE_NOT_FOUND")
                return report
            report.exists = True

            # Layer 2: size — adaptive: 1 MB for video, 50 KB for image, 5 KB for audio
            size = Path(artifact).stat().st_size
            report.size_bytes = size
            min_size = self.settings.min_size_bytes
            if expected_meta and expected_meta.get("size_bytes_min"):
                min_size = int(expected_meta["size_bytes_min"])
            # We'll re-evaluate min_size after magic-byte detection below
            # (deferred to layer 3)

            # Layer 3: magic bytes
            kind, mime = detect_file_type(artifact)
            report.mime_type = mime
            if kind == "html":
                report.issues.append("E_HTML_ERROR_PAGE")
            elif kind is None:
                report.issues.append("E_MAGIC_BYTES_UNKNOWN")

            # Re-evaluate min_size based on file kind (images can be small, videos need 1 MB, audio needs 5 KB)
            if not (expected_meta and expected_meta.get("size_bytes_min")):
                if kind in ("jpg", "png", "gif", "webp"):
                    min_size = 50 * 1024  # 50 KB for images
                elif kind in ("mp3", "ogg"):
                    min_size = 5 * 1024  # 5 KB for audio
                else:
                    min_size = self.settings.min_size_bytes  # 1 MB default for video
            if size < min_size:
                report.issues.append("E_SIZE_TOO_SMALL")

            # Layer 4: ffprobe
            probe_json = await probe(artifact)
            if probe_json is None:
                report.issues.append("E_FFPROBE_FAILED")
            else:
                summary = extract_probe_summary(probe_json)
                report.has_video_stream = summary["has_video_stream"]
                report.has_audio_stream = summary["has_audio_stream"]
                report.duration_s = summary["duration_s"]
                report.codec_video = summary["codec_video"]
                report.codec_audio = summary["codec_audio"]
                report.container = summary["container"]

                # Layer 5: duration + streams
                if report.duration_s is None or report.duration_s <= 0:
                    report.issues.append("E_DURATION_ZERO")
                if not (report.has_video_stream or report.has_audio_stream):
                    report.issues.append("E_NO_STREAMS")

                # Layer 6: moov atom (MP4 only) — only flag if ffprobe ALSO failed
                # Many streaming-muxed MP4s have moov at the END of the file (outside our 4MB search window)
                # which is perfectly valid. ffprobe will succeed if moov is anywhere. So only flag
                # E_MOOV_MISSING when ffprobe couldn't parse the container either.
                if kind == "mp4" and not has_moov_atom(artifact) and not (report.has_video_stream or report.has_audio_stream):
                    report.issues.append("E_MOOV_MISSING")

                # Layer 7 (bonus): duration match against expected
                if expected_meta and expected_meta.get("duration_s"):
                    expected_dur = float(expected_meta["duration_s"])
                    actual_dur = report.duration_s or 0.0
                    if abs(expected_dur - actual_dur) > 2.0:
                        report.issues.append("E_DURATION_MISMATCH")

            # Bonus: integrity decode (slow, optional — only for small files where it won't block)
            # Skip for files > 50 MB to avoid long ffmpeg pass through the whole file
            if (not report.issues
                    and report.mime_type
                    and report.mime_type.startswith(("video/", "audio/"))
                    and report.size_bytes < 50 * 1024 * 1024):
                if not await integrity_decode(artifact):
                    report.issues.append("E_INTEGRITY_DECODE_FAILED")

            report.integrity_ok = len(report.issues) == 0
            return report
        except Exception as e:
            report.issues.append(f"E_EXCEPTION:{type(e).__name__}")
            report.integrity_ok = False
            log.error("verifier_exception", artifact=str(artifact), error=str(e))
            return report
