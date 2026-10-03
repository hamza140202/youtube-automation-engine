"""Rednote / Xiaohongshu extractor — fallback chain verified live 2026-10-03.

KEY DISCOVERY (from Task T5 research):
- Raw curl/yt-dlp gets a "page not found" placeholder HTML from datacenter IPs
- JoeanAmier/XHS-Downloader uses curl_cffi with chrome146 impersonation — fakes
  Chrome's TLS/JA3 fingerprint, bypasses the TLS-fingerprint bot wall
- XHS-Downloader self-signs X-s/X-t to hit edith.xiaohongshu.com/api/sns/web/v1/feed
  (the real feed API) instead of the walled explore HTML
- Cookie is OPTIONAL for public notes — without it, low-res videos download

Chain (priority order):
  1. JoeanAmier/XHS-Downloader (subprocess)  (PRIMARY — verified)
  2. Direct curl_cffi request (if XHS-Downloader binary not present)
  3. yt-dlp XiaoHongShu (fallback — usually returns "page not found")
  4. Honest empty: datacenter_ip_walled
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from avd.extractors.base import BaseExtractor
from avd.extractors.registry import ExtractorRegistry
from avd.models import DownloadMetadata, ExtractOutcome, ExtractorMeta
from avd.utils.fs import detect_file_type, ensure_dir, safe_unlink
from avd.utils.http import fetch_text, stream_download
from avd.utils.logging import get_logger

log = get_logger("avd.extractors.rednote")

REDNOTE_URL_PATTERNS = [
    r"^https?://(?:www\.)?xiaohongshu\.com/.+",
    r"^https?://xhslink\.com/.+",
]

REDNOTE_CDN_ALLOWLIST = (
    "sns-video.xhscdn.com",
    "sns-img.xhscdn.com",
    "sns-video-qc.xhscdn.com",
    "xhscdn.com",
    "xiaohongshu.com",
)

# Candidate paths for the XHS-Downloader repo (if user has cloned it)
_XHS_DOWNLOADER_PATHS = [
    "/home/z/my-project/XHS-Downloader",
    "/home/z/agent-video-downloader/XHS-Downloader",
    os.path.expanduser("~/XHS-Downloader"),
    "/tmp/XHS-Downloader",
]


def _is_allowed_cdn(url: str) -> bool:
    return any(host in url.lower() for host in REDNOTE_CDN_ALLOWLIST)


def _extract_note_id(url: str) -> str | None:
    """Extract the 24-char hex note ID from a XHS URL."""
    m = re.search(r"/(?:explore|discovery/item|user/profile/[^/]+)/([a-z0-9]{24,})", url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/(?:explore|discovery/item)/([a-zA-Z0-9]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _find_xhs_downloader() -> str | None:
    """Find a cloned XHS-Downloader repo."""
    for p in _XHS_DOWNLOADER_PATHS:
        main_py = Path(p) / "main.py"
        if main_py.exists():
            return p
    return None


async def _try_xhs_downloader_subprocess(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 1: Invoke JoeanAmier/XHS-Downloader via subprocess.

    AUTO-BOOTSTRAPS: If XHS-Downloader isn't found, clones it on first call
    so the user never needs to manually install anything.
    """
    note_id = _extract_note_id(url)
    if not note_id:
        return ExtractOutcome(ok=False, extractor_name="rednote:xhs", error="no_note_id")

    repo_path = _find_xhs_downloader()
    if not repo_path:
        # AUTO-BOOTSTRAP: clone XHS-Downloader on first call
        log.info("rednote_auto_bootstrap_start", url=url)
        from avd.bootstrap import ensure_xhs_downloader

        repo_path = ensure_xhs_downloader()
        if not repo_path:
            return ExtractOutcome(
                ok=False,
                extractor_name="rednote:xhs",
                error="xhs_downloader_auto_bootstrap_failed",
            )
        log.info("rednote_auto_bootstrap_done", path=repo_path)
        # Update the module-level search list so future calls find it without re-checking
        if repo_path not in _XHS_DOWNLOADER_PATHS:
            _XHS_DOWNLOADER_PATHS.insert(0, repo_path)

    out_dir = dest if dest.is_dir() else dest.parent
    ensure_dir(out_dir)

    # Invoke via the Python API directly (bypass main.py's GUI deps)
    # Per Task T5 research: from source.application.app import XHS
    code = f"""
import asyncio, sys
sys.path.insert(0, "{repo_path}")
from source.application.app import XHS

async def main():
    xhs = XHS(work_path="{out_dir}", folder_name=".", cookie="", timeout=20, max_retry=2,
               image_download=True, video_download=True)
    async with xhs:
        await xhs.extract_cli("{url}", download=True)

asyncio.run(main())
"""
    cmd = [sys.executable, "-c", code]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repo_path,
        )
        stdout, stderr = await proc.communicate()
        # Log subprocess output for debugging
        stdout_text = stdout.decode(errors="replace") if stdout else ""
        stderr_text = stderr.decode(errors="replace") if stderr else ""
        if stdout_text:
            log.info("xhs_subprocess_stdout", output=stdout_text[-1000:])
        if stderr_text:
            log.info("xhs_subprocess_stderr", output=stderr_text[-1000:])
        if proc.returncode != 0:
            return ExtractOutcome(ok=False, extractor_name="rednote:xhs", error=f"subprocess_failed: {stderr_text[:500]}")
        # Parse XHS-Downloader's output to verify it actually downloaded something
        # Output format: "共处理 N 个作品，成功 X 个，失败 Y 个，跳过 Z 个"
        import re as _re
        success_match = _re.search(r"成功\s*(\d+)\s*个", stdout_text)
        failed_match = _re.search(r"失败\s*(\d+)\s*个", stdout_text)
        success_count = int(success_match.group(1)) if success_match else 0
        failed_count = int(failed_match.group(1)) if failed_match else 0
        if success_count == 0:
            # XHS-Downloader reported 0 successes — note is bot-walled / private / not video
            reason = "data_fetch_failed"
            if "获取数据失败" in stdout_text:
                reason = "datacenter_ip_walled"
            return ExtractOutcome(ok=False, extractor_name="rednote:xhs", error=reason, raw_info={"stdout": stdout_text[-500:], "success_count": success_count, "failed_count": failed_count})
    except Exception as e:
        return ExtractOutcome(ok=False, extractor_name="rednote:xhs", error=f"subprocess_exception: {e}")

    # Find the downloaded file — XHS-Downloader writes to <repo_path>/Volume/Download/<filename>
    # regardless of `work_path` argument (the XHS class uses its own VOLUME constant)
    # Filename format: <date>_<author>_<title>_<index>.mp4 or .jpeg
    # Look in BOTH the requested out_dir AND the XHS-Downloader's own Volume dir
    out_dir_path = Path(out_dir)
    repo_volume_path = Path(repo_path) / "Volume"
    search_paths = [out_dir_path, repo_volume_path, out_dir_path / "Volume", out_dir_path / "Download", repo_volume_path / "Download"]
    candidates: list[Path] = []
    for sp in search_paths:
        if not sp.exists():
            continue
        candidates.extend(sp.rglob("*.mp4"))
        candidates.extend(sp.rglob("*.jpeg"))
        candidates.extend(sp.rglob("*.jpg"))
        candidates.extend(sp.rglob("*.png"))
    # Filter out the XHS-Downloader's own .db cache files
    candidates = [c for c in candidates if not c.name.endswith(".db")]
    if not candidates:
        return ExtractOutcome(ok=False, extractor_name="rednote:xhs", error="no_artifact_after_subprocess")

    # Pick the most recent file
    artifact = max(candidates, key=lambda p: p.stat().st_mtime)
    kind, _ = detect_file_type(artifact)
    if kind not in ("mp4", "jpg", "png", None):
        safe_unlink(artifact)
        return ExtractOutcome(ok=False, extractor_name="rednote:xhs", error=f"magic_byte_{kind}")

    # If artifact is not in our requested out_dir, copy it there
    target_path = out_dir_path / f"{note_id}{artifact.suffix}"
    if artifact.parent != out_dir_path:
        try:
            import shutil as _sh
            _sh.copy(artifact, target_path)
            artifact = target_path
        except Exception as e:
            log.warning("xhs_copy_failed", error=str(e), src=str(artifact), dest=str(target_path))

    return ExtractOutcome(
        ok=True,
        artifact_path=Path(artifact),
        metadata=DownloadMetadata(source_platform_post_id=note_id),
        extractor_name="rednote:xhs",
    )


async def _try_direct_curl_cffi(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 2: Direct curl_cffi request with chrome impersonation (if curl_cffi installed)."""
    note_id = _extract_note_id(url)
    if not note_id:
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error="no_note_id")

    try:
        from curl_cffi import requests as cc_requests
    except ImportError:
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error="curl_cffi_not_installed")

    try:
        r = cc_requests.get(url, impersonate="chrome", timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error=f"http_{r.status_code}")
        html = r.text
    except Exception as e:
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error=str(e))

    # Look for sns-video-*.xhscdn.com URL
    m = re.search(r"https://sns-video[^\"' ]+\.mp4", html)
    if not m:
        # Try og:video meta tag
        m = re.search(r'<meta[^>]+property="og:video[^"]*"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if not m:
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error="no_video_in_html")

    vid_url = m.group(1) if m else None
    if not vid_url:
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error="no_video_url_extracted")

    if not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error="cdn_not_allowed")

    out_path = dest if str(dest).endswith(".mp4") else dest / f"{note_id}.mp4"
    ensure_dir(out_path.parent)
    # Use curl_cffi for the download too (TLS-fingerprint bypass)
    try:
        r = cc_requests.get(vid_url, impersonate="chrome", timeout=60, allow_redirects=True)
        if r.status_code != 200 or len(r.content) == 0:
            return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error=f"download_http_{r.status_code}")
        with open(out_path, "wb") as f:
            f.write(r.content)
    except Exception as e:
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error=f"download_exception_{e}")

    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="rednote:curl_cffi", error=f"magic_byte_{kind}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(source_platform_post_id=note_id),
        extractor_name="rednote:curl_cffi",
    )


async def _try_ytdlp(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 3: yt-dlp XiaoHongShu — usually fails ('page not found' placeholder from datacenter IP)."""
    note_id = _extract_note_id(url)
    if not note_id:
        return ExtractOutcome(ok=False, extractor_name="rednote:ytdlp", error="no_note_id")
    try:
        import yt_dlp
    except ImportError:
        return ExtractOutcome(ok=False, extractor_name="rednote:ytdlp", error="yt_dlp_not_installed")
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{note_id}.mp4"
    ensure_dir(out_path.parent)
    ytdlp_opts = {
        "outtmpl": str(out_path),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/",
        },
    }
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return ExtractOutcome(ok=False, extractor_name="rednote:ytdlp", error="ytdlp_extract_failed")
            return ExtractOutcome(
                ok=True,
                artifact_path=Path(out_path),
                metadata=DownloadMetadata(
                    source_platform_post_id=note_id,
                    title=info.get("title"),
                    author=info.get("uploader"),
                    duration_s=info.get("duration"),
                ),
                extractor_name="rednote:ytdlp",
            )
    except Exception as e:
        msg = str(e)
        if "No video formats found" in msg:
            return ExtractOutcome(ok=False, extractor_name="rednote:ytdlp", error="datacenter_ip_walled")
        return ExtractOutcome(ok=False, extractor_name="rednote:ytdlp", error=msg)


class RednoteExtractor(BaseExtractor):
    meta = ExtractorMeta(
        name="rednote",
        priority=150,
        url_patterns=REDNOTE_URL_PATTERNS,
        platforms=["rednote"],
        description="Rednote/Xiaohongshu fallback chain: XHS-Downloader (curl_cffi chrome impersonation) → curl_cffi direct → yt-dlp",
    )

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        opts = opts or {}
        if "xhslink.com/" in url:
            from avd.utils.http import head_url
            expanded = await head_url(url)
            if expanded:
                url = expanded
        for slot in (_try_xhs_downloader_subprocess, _try_direct_curl_cffi, _try_ytdlp):
            try:
                outcome = await slot(url, dest, opts)
                if outcome.ok:
                    log.info("rednote_slot_ok", slot=outcome.extractor_name, url=url)
                    return outcome
                log.info("rednote_slot_fail", slot=outcome.extractor_name, error=outcome.error, url=url)
            except Exception as e:
                log.warning("rednote_slot_exception", slot=slot.__name__, error=str(e))
        return ExtractOutcome(ok=False, extractor_name="rednote:chain", error="datacenter_ip_walled")


ExtractorRegistry.register(RednoteExtractor())
