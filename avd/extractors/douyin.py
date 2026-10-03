"""Douyin extractor — fallback chain verified live 2026-10-03.

KEY DISCOVERY (from Task T4 research):
api.douyin.wtf is Evil0ctal's self-hosted Douyin_TikTok_Download_API v5
running as a public demo. The operator publishes demo credentials via
/api/v1/auth/demo (anonymous), then /api/v1/auth/login gives a 7-day
session cookie. The operator's CloakBrowser (Chromium, 572 active minted
Douyin identities) does all signature/cookie-minting heavy lifting on
their side, not ours.

Chain (priority order):
  1. api.douyin.wtf public demo  (PRIMARY — zero-config, verified)
  2. AVD_DOUYIN_DTK_URL self-hosted sidecar  (production path)
  3. yt-dlp Douyin + self-minted cookies  (will fail from datacenter IP)
  4. Honest empty: datacenter_ip_walled
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from avd.config import get_settings
from avd.extractors.base import BaseExtractor
from avd.extractors.registry import ExtractorRegistry
from avd.models import DownloadMetadata, ExtractOutcome, ExtractorMeta
from avd.utils.fs import detect_file_type, ensure_dir, safe_unlink
from avd.utils.http import fetch_json, stream_download
from avd.utils.logging import get_logger

log = get_logger("avd.extractors.douyin")

DOUYIN_URL_PATTERNS = [
    r"^https?://(?:www\.)?douyin\.com/.+",
    r"^https?://v\.douyin\.com/.+",
    r"^https?://iesdouyin\.com/.+",
]

# Per Task T4: add zjcdn.com (verified Douyin CDN)
DOUYIN_CDN_ALLOWLIST = (
    "douyin.com",
    "douyinvod.com",
    "bytecdn.cn",
    "byteimg.com",
    "douyinpic.com",
    "aweme.snssdk.com",
    "zjcdn.com",
    "douyinvod.com",
    "amemv.com",
    "bytedance.com",
    "snssdk.com",
)

DEMO_BASE_URL = "https://api.douyin.wtf"


def _is_allowed_cdn(url: str) -> bool:
    return any(host in url.lower() for host in DOUYIN_CDN_ALLOWLIST)


def _extract_aweme_id(url: str) -> str | None:
    """Extract the aweme_id (numeric video ID) from a Douyin URL."""
    m = re.search(r"/video/(\d{10,})", url)
    if m:
        return m.group(1)
    m = re.search(r"modal_id=(\d{10,})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{10,})\b", url)
    if m:
        return m.group(1)
    return None


# Cache for the demo session cookie
_demo_cookie_cache: dict[str, Any] = {}


async def _dtk_demo_login() -> str | None:
    """Log in to the public api.douyin.wtf demo. Returns cookie header value.

    Cached for 1 hour (well under the 7-day TTL).
    """
    import time
    now = time.time()
    if _demo_cookie_cache.get("expires_at", 0) > now and _demo_cookie_cache.get("cookie"):
        return _demo_cookie_cache["cookie"]

    # Fetch demo credentials (anonymous)
    status, data, _ = await fetch_json(f"{DEMO_BASE_URL}/api/v1/auth/demo", headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        log.warning("dtk_demo_creds_failed", status=status)
        return None
    creds = data.get("data") or {}
    username = creds.get("username")
    password = creds.get("password")
    if not (username and password):
        log.warning("dtk_demo_creds_missing", data=str(data)[:200])
        return None

    # Login — capture Set-Cookie
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(
                f"{DEMO_BASE_URL}/api/v1/auth/login",
                json={"username": username, "password": password},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/json",
                    "Origin": DEMO_BASE_URL,
                    "Referer": f"{DEMO_BASE_URL}/",
                },
            )
            if r.status_code != 200:
                log.warning("dtk_demo_login_failed", status=r.status_code, body=r.text[:200])
                return None
            # Extract cookies
            cookies = r.headers.get_list("set-cookie")
            if not cookies:
                # Try the cookies jar
                if r.cookies:
                    cookie_str = "; ".join(f"{k}={v}" for k, v in r.cookies.items())
                    _demo_cookie_cache["cookie"] = cookie_str
                    _demo_cookie_cache["expires_at"] = now + 3600
                    return cookie_str
                return None
            # Parse Set-Cookie headers
            cookie_parts = []
            for c in cookies:
                # Take just the name=value part
                m = re.match(r"([^=]+=[^;]+)", c)
                if m:
                    cookie_parts.append(m.group(1))
            cookie_str = "; ".join(cookie_parts)
            _demo_cookie_cache["cookie"] = cookie_str
            _demo_cookie_cache["expires_at"] = now + 3600
            return cookie_str
        except Exception as e:
            log.warning("dtk_demo_login_exception", error=str(e))
            return None


async def _try_dtk_demo(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 1: api.douyin.wtf public demo (zero-config)."""
    aweme_id = _extract_aweme_id(url)
    if not aweme_id:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error="no_aweme_id")

    cookie = await _dtk_demo_login()
    if not cookie:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error="dtk_demo_login_failed")

    # Hit the parse endpoint with wait=25 for synchronous result
    parse_url = f"{DEMO_BASE_URL}/api/v1/douyin/video?aweme_id={aweme_id}&wait=25"
    status, data, raw = await fetch_json(
        parse_url,
        headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie, "Referer": f"{DEMO_BASE_URL}/"},
    )
    if status != 200 or not isinstance(data, dict):
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error=f"http_{status}")

    # Navigate to the video URL — DTK returns nested data
    # Per T4 research: data.data.media.video.urls[0] is the primary URL
    d_data = data.get("data") or {}
    media = d_data.get("media") or {}
    video = media.get("video") or {}
    urls = video.get("urls") or []
    if not urls:
        # Try alt paths
        aweme_detail = d_data.get("aweme_detail") or {}
        video_obj = aweme_detail.get("video") or {}
        play_addr = video_obj.get("play_addr") or {}
        urls = play_addr.get("url_list") or []
    if not urls:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error="no_video_url", raw_info={"raw": raw[:500] if raw else None})

    vid_url = urls[0]
    if not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error="cdn_not_allowed")

    out_path = dest if str(dest).endswith(".mp4") else dest / f"{aweme_id}.mp4"
    ensure_dir(out_path.parent)

    ok, _, err = await stream_download(
        vid_url,
        str(out_path),
        headers={"Referer": "https://www.douyin.com/", "User-Agent": "Mozilla/5.0"},
    )
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error=f"download_{err}")

    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk_demo", error=f"magic_byte_{kind}")

    metadata = DownloadMetadata(
        source_platform_post_id=aweme_id,
        title=d_data.get("title") or (aweme_detail.get("desc") if 'aweme_detail' in dir() else None),
        author=str(d_data.get("author_id") or ""),
    )
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=metadata,
        extractor_name="douyin:dtk_demo",
        raw_info=data,
    )


async def _try_dtk_sidecar(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 2: Self-hosted Evil0ctal DTK API sidecar (only if AVD_DOUYIN_DTK_URL set)."""
    s = get_settings()
    if not s.douyin_dtk_url:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error="dtk_not_configured")
    aweme_id = _extract_aweme_id(url)
    if not aweme_id:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error="no_aweme_id")
    api_url = f"{s.douyin_dtk_url.rstrip('/')}/api/v1/douyin/web/fetch_one_video?aweme_id={aweme_id}"
    status, data, _ = await fetch_json(api_url, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error=f"http_{status}")
    detail = (data.get("data") or {}).get("aweme_detail") or {}
    video_obj = detail.get("video") or {}
    play_addr = video_obj.get("play_addr") or {}
    urls = play_addr.get("url_list") or []
    if not urls:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error="no_video_url")
    vid_url = urls[0]
    if not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error="cdn_not_allowed")
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{aweme_id}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(vid_url, str(out_path), headers={"Referer": "https://www.douyin.com/"})
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error=f"download_{err}")
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="douyin:dtk", error=f"magic_byte_{kind}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(
            source_platform_post_id=aweme_id,
            title=detail.get("desc"),
            author=str((detail.get("author") or {}).get("uid") or ""),
        ),
        extractor_name="douyin:dtk",
        raw_info=data,
    )


async def _try_ytdlp(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 3: yt-dlp Douyin with anonymous cookies (almost always walled from datacenter IP)."""
    aweme_id = _extract_aweme_id(url)
    if not aweme_id:
        return ExtractOutcome(ok=False, extractor_name="douyin:ytdlp", error="no_aweme_id")
    try:
        import yt_dlp
    except ImportError:
        return ExtractOutcome(ok=False, extractor_name="douyin:ytdlp", error="yt_dlp_not_installed")
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{aweme_id}.mp4"
    ensure_dir(out_path.parent)
    ytdlp_opts = {
        "outtmpl": str(out_path),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.douyin.com/",
        },
    }
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return ExtractOutcome(ok=False, extractor_name="douyin:ytdlp", error="ytdlp_extract_failed")
            return ExtractOutcome(
                ok=True,
                artifact_path=Path(out_path),
                metadata=DownloadMetadata(
                    source_platform_post_id=aweme_id,
                    title=info.get("title"),
                    author=info.get("uploader"),
                    duration_s=info.get("duration"),
                ),
                extractor_name="douyin:ytdlp",
            )
    except Exception as e:
        msg = str(e)
        if "Fresh cookies" in msg or "cookies" in msg.lower():
            return ExtractOutcome(ok=False, extractor_name="douyin:ytdlp", error="datacenter_ip_walled")
        return ExtractOutcome(ok=False, extractor_name="douyin:ytdlp", error=msg)


class DouyinExtractor(BaseExtractor):
    meta = ExtractorMeta(
        name="douyin",
        priority=160,
        url_patterns=DOUYIN_URL_PATTERNS,
        platforms=["douyin"],
        description="Douyin fallback chain: api.douyin.wtf demo → self-hosted DTK → yt-dlp",
    )

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        opts = opts or {}
        if "v.douyin.com/" in url:
            from avd.utils.http import head_url
            expanded = await head_url(url)
            if expanded:
                url = expanded
        for slot in (_try_dtk_demo, _try_dtk_sidecar, _try_ytdlp):
            try:
                outcome = await slot(url, dest, opts)
                if outcome.ok:
                    log.info("douyin_slot_ok", slot=outcome.extractor_name, url=url)
                    return outcome
                log.info("douyin_slot_fail", slot=outcome.extractor_name, error=outcome.error, url=url)
            except Exception as e:
                log.warning("douyin_slot_exception", slot=slot.__name__, error=str(e))
        return ExtractOutcome(
            ok=False,
            extractor_name="douyin:chain",
            error="datacenter_ip_walled",
        )


ExtractorRegistry.register(DouyinExtractor())
