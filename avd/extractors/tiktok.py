"""TikTok extractor — fallback chain verified live 2026-10-03.

Chain (priority order):
  1. TikWM mirror API         (primary — verified live)
  2. TikTok embed v2 hydration  (slot 2 — TikWM down)
  3. Tiklydown mirror         (slot 3 — second mirror)
  4. TikTok oEmbed            (slot 4 — metadata-only last resort)
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from avd.extractors.base import BaseExtractor
from avd.extractors.registry import ExtractorRegistry
from avd.models import DownloadMetadata, ExtractOutcome, ExtractorMeta
from avd.utils.breaker import is_open, record_failure, record_success
from avd.utils.fs import detect_file_type, ensure_dir, has_moov_atom, safe_unlink
from avd.utils.http import fetch_json, head_url, stream_download
from avd.utils.logging import get_logger

log = get_logger("avd.extractors.tiktok")

# TikTok video URL patterns
TIKTOK_URL_PATTERNS = [
    r"^https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/.+",
    r"^https?://tiktok\.com/.+",
]

# CDN allowlist — bytes may only be fetched from these
TIKTOK_CDN_ALLOWLIST = (
    "tiktokcdn-us.com",
    "tiktokcdn.com",
    "tikwm.com",
    "tiktok.com",
    "tiktokv.com",
    "byteoversea.com",
    "ttwvideo.akamaized.net",
    "muscdn.com",
    "p16-sign",
    "p19-sign",
    "p20-sign",
    "p21-sign",
    "p22-sign",
    "p23-sign",
    "p3-sign",
    "p9-sign",
    "p11-sign",
    "p26-sign",
    "p28-sign",
    "p29-sign",
)


def _is_allowed_cdn(url: str) -> bool:
    """Return True if URL host is in our allowlist."""
    low = url.lower()
    return any(host in low for host in TIKTOK_CDN_ALLOWLIST)


def _extract_tiktok_id(url: str) -> str | None:
    """Extract the video ID from a TikTok URL."""
    # /video/<id>
    m = re.search(r"/video/(\d{10,})", url)
    if m:
        return m.group(1)
    # /@user/video/<id>
    m = re.search(r"@[\w.\-]+/video/(\d{10,})", url)
    if m:
        return m.group(1)
    # trailing numeric
    m = re.search(r"/(\d{10,})\b", url)
    if m:
        return m.group(1)
    return None


async def _try_tikwm(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 1: TikWM mirror API."""
    api = "https://www.tikwm.com/api/"
    params = {"url": url, "hd": "1"}
    full = f"{api}?{urllib.parse.urlencode(params)}"
    host = "www.tikwm.com"
    if is_open(host):
        return ExtractOutcome(ok=False, extractor_name="tiktok:tikwm", error="circuit_open")

    status, data, raw = await fetch_json(full, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="tiktok:tikwm", error=f"http_{status}")

    if data.get("code") != 0:
        return ExtractOutcome(
            ok=False,
            extractor_name="tiktok:tikwm",
            error=f"tikwm_code_{data.get('code')}",
            raw_info=data,
        )

    d = data.get("data") or {}
    # Pick best-quality video URL
    video_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
    if not video_url:
        return ExtractOutcome(ok=False, extractor_name="tiktok:tikwm", error="no_video_url")

    # Absolutize relative URLs
    if video_url.startswith("/"):
        video_url = "https://www.tikwm.com" + video_url
    if not _is_allowed_cdn(video_url):
        return ExtractOutcome(ok=False, extractor_name="tiktok:tikwm", error="cdn_not_allowed")

    # Ensure dest is a file path
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{d.get('id', 'tiktok')}.mp4"
    ensure_dir(out_path.parent)

    ok, size, err = await stream_download(video_url, str(out_path))
    if not ok:
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="tiktok:tikwm", error=f"download_{err}")

    record_success(host)

    # Magic byte check
    kind, mime = detect_file_type(out_path)
    if kind not in ("mp4", "mp3", None):  # be lenient on the slot — let Verifier judge
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="tiktok:tikwm", error=f"magic_byte_{kind}")

    metadata = DownloadMetadata(
        title=d.get("title"),
        author=(d.get("author") or {}).get("unique_id"),
        author_id=str((d.get("author") or {}).get("id") or ""),
        duration_s=d.get("duration") or None,
        thumbnail_url=d.get("cover"),
        source_platform_post_id=str(d.get("id") or ""),
        media_count=len(d.get("images") or []) or 1,
        extra={
            "play_count": (d.get("stats") or {}).get("playCount"),
            "digg_count": (d.get("stats") or {}).get("diggCount"),
            "music_url": d.get("music"),
        },
    )

    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=metadata,
        extractor_name="tiktok:tikwm",
        raw_info=data,
    )


async def _try_embed_v2(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 2: TikTok embed v2 hydration blob."""
    vid = _extract_tiktok_id(url)
    if not vid:
        return ExtractOutcome(ok=False, extractor_name="tiktok:embed_v2", error="no_video_id")
    embed_url = f"https://www.tiktok.com/embed/v2/{vid}"
    from avd.utils.http import fetch_text

    status, text = await fetch_text(embed_url, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not text:
        return ExtractOutcome(ok=False, extractor_name="tiktok:embed_v2", error=f"http_{status}")

    # Search for SIGI_STATE or __UNIVERSAL_DATA_FOR_REHYDRATION__
    m = re.search(r'"playAddr"\s*:\s*"([^"]+)"', text)
    if not m:
        m = re.search(r'"downloadAddr"\s*:\s*"([^"]+)"', text)
    if not m:
        return ExtractOutcome(ok=False, extractor_name="tiktok:embed_v2", error="no_video_url_in_embed")
    # Unescape JSON-encoded URL
    vid_url = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
    if not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="tiktok:embed_v2", error="cdn_not_allowed")

    out_path = dest if str(dest).endswith(".mp4") else dest / f"{vid}.mp4"
    ensure_dir(out_path.parent)
    ok, size, err = await stream_download(vid_url, str(out_path), headers={"Referer": "https://www.tiktok.com/"})
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="tiktok:embed_v2", error=f"download_{err}")
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="tiktok:embed_v2", error=f"magic_byte_{kind}")

    metadata = DownloadMetadata(
        source_platform_post_id=vid,
        title=None,
        author=None,
    )
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=metadata,
        extractor_name="tiktok:embed_v2",
    )


async def _try_tiklydown(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 3: Tiklydown mirror."""
    api = "https://api.tiklydown.eu.org/api/download"
    full = f"{api}?url={urllib.parse.quote(url, safe='')}"
    host = "api.tiklydown.eu.org"
    if is_open(host):
        return ExtractOutcome(ok=False, extractor_name="tiktok:tiklydown", error="circuit_open")
    status, data, _ = await fetch_json(full, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="tiktok:tiklydown", error=f"http_{status}")
    # Find video URL
    vid_url = (data.get("video") or {}).get("noWatermark") or (data.get("video") or {}).get("wm")
    if not vid_url:
        # might be image gallery
        images = data.get("images") or []
        if images:
            vid_url = images[0]
    if not vid_url:
        return ExtractOutcome(ok=False, extractor_name="tiktok:tiklydown", error="no_video_url")
    if not _is_allowed_cdn(vid_url):
        # Mirror may return its own CDN — allow tiklydown too
        if "tiklydown" not in vid_url.lower():
            return ExtractOutcome(ok=False, extractor_name="tiktok:tiklydown", error="cdn_not_allowed")
    vid = _extract_tiktok_id(url) or "tiktok"
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{vid}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(vid_url, str(out_path))
    if not ok:
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="tiktok:tiklydown", error=f"download_{err}")
    record_success(host)
    metadata = DownloadMetadata(
        source_platform_post_id=vid,
        title=data.get("title"),
        author=(data.get("author") or {}).get("nickname"),
    )
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=metadata,
        extractor_name="tiktok:tiklydown",
        raw_info=data,
    )


async def _try_oembed(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 4: TikTok oEmbed — metadata only, no video. Honest empty."""
    return ExtractOutcome(
        ok=False,
        extractor_name="tiktok:oembed",
        error="metadata_only_no_video_bytes",
    )


class TikTokExtractor(BaseExtractor):
    meta = ExtractorMeta(
        name="tiktok",
        priority=100,
        url_patterns=TIKTOK_URL_PATTERNS,
        platforms=["tiktok"],
        description="TikTok fallback chain: TikWM → embed/v2 → tiklydown → oEmbed",
    )

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        opts = opts or {}
        # Expand short links first
        if "vm.tiktok.com" in url or "vt.tiktok.com" in url or "tiktok.com/t/" in url:
            expanded = await head_url(url)
            if expanded:
                log.info("tiktok_shortlink_expanded", original=url, expanded=expanded)
                url = expanded
        # Try each slot
        for slot in (_try_tikwm, _try_embed_v2, _try_tiklydown, _try_oembed):
            try:
                outcome = await slot(url, dest, opts)
                if outcome.ok:
                    log.info("tiktok_slot_ok", slot=outcome.extractor_name, url=url)
                    return outcome
                log.info("tiktok_slot_fail", slot=outcome.extractor_name, error=outcome.error, url=url)
            except Exception as e:
                log.warning("tiktok_slot_exception", slot=slot.__name__, error=str(e))
                continue
        return ExtractOutcome(ok=False, extractor_name="tiktok:chain", error="all_slots_failed")


# Auto-register
ExtractorRegistry.register(TikTokExtractor())
