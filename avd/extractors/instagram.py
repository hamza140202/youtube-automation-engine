"""Instagram extractor — fallback chain verified live 2026-10-03.

KEY DISCOVERY (from Task T3 research):
Instagram's "datacenter IP wall" is bypassed by setting
User-Agent: facebookexternalhit/1.1 — Facebook's external link crawler
which Instagram allow-lists for SSR access. yt-dlp 2026.08.19 with
this UA + --no-cookies reliably downloads Instagram reels/posts.

Chain (priority order):
  1. yt-dlp + facebookexternalhit UA              (PRIMARY — verified, easiest)
  2. embed/captioned/ + facebookexternalhit UA + manual CDN curl  (stdlib-only fallback)
  3. embed/captioned/ + Instagram Android-app UA  (slot 3, same approach diff UA)
  4. embed/captioned/ + Instagram iOS-app UA      (slot 4)
  5. Third-party mirror (ddinstagram, etc.)       (last resort)
  6. Honest empty: datacenter_ip_walled           (terminal)
"""
from __future__ import annotations

import re
from pathlib import Path

from avd.extractors.base import BaseExtractor
from avd.extractors.registry import ExtractorRegistry
from avd.models import DownloadMetadata, ExtractOutcome, ExtractorMeta
from avd.utils.fs import detect_file_type, ensure_dir, safe_unlink
from avd.utils.http import fetch_text, stream_download
from avd.utils.logging import get_logger

log = get_logger("avd.extractors.instagram")

INSTAGRAM_URL_PATTERNS = [
    r"^https?://(?:www\.)?instagram\.com/.+",
    r"^https?://instagr\.am/.+",
]

INSTAGRAM_CDN_ALLOWLIST = (
    "cdninstagram.com",
    "fbcdn.net",
    "fbcdn.com",
    "scontent",
    "instagram.com",
)

# Per Task T3 — the facebookexternalhit UA bypasses Instagram's datacenter IP wall
FB_EXTERNAL_HIT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
ANDROID_IG_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Instagram 250.0.0.20 Android Version 30"
IOS_IG_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Instagram 250.0.0.20"


def _is_allowed_cdn(url: str) -> bool:
    return any(host in url.lower() for host in INSTAGRAM_CDN_ALLOWLIST)


def _extract_shortcode(url: str) -> str | None:
    m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})", url)
    if m:
        return m.group(1)
    return None


async def _try_ytdlp_facebookexternalhit(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 1: yt-dlp with facebookexternalhit UA — verified working from datacenter IP."""
    code = _extract_shortcode(url)
    if not code:
        return ExtractOutcome(ok=False, extractor_name="instagram:ytdlp", error="no_shortcode")
    try:
        import yt_dlp
    except ImportError:
        return ExtractOutcome(ok=False, extractor_name="instagram:ytdlp", error="yt_dlp_not_installed")

    out_path = dest if str(dest).endswith((".mp4", ".jpg", ".webp")) else dest / f"{code}.mp4"
    ensure_dir(out_path.parent)
    ytdlp_opts = {
        "outtmpl": str(out_path),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "no_cookies": True,
        "no_cache_dir": True,
        "http_headers": {
            "User-Agent": FB_EXTERNAL_HIT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return ExtractOutcome(ok=False, extractor_name="instagram:ytdlp", error="ytdlp_extract_failed")
            kind, _ = detect_file_type(out_path)
            if kind not in ("mp4", "jpg", "webp", None):
                safe_unlink(out_path)
                return ExtractOutcome(ok=False, extractor_name="instagram:ytdlp", error=f"magic_byte_{kind}")
            return ExtractOutcome(
                ok=True,
                artifact_path=Path(out_path),
                metadata=DownloadMetadata(
                    source_platform_post_id=code,
                    title=info.get("title"),
                    author=info.get("uploader") or info.get("channel"),
                    duration_s=info.get("duration"),
                    thumbnail_url=info.get("thumbnail"),
                ),
                extractor_name="instagram:ytdlp",
            )
    except Exception as e:
        return ExtractOutcome(ok=False, extractor_name="instagram:ytdlp", error=str(e))


async def _try_embed_captioned_with_ua(url: str, dest: Path, opts: dict, ua: str, slot_name: str) -> ExtractOutcome:
    """Common impl for slots 2-4: embed/captioned/ with various UAs + manual CDN curl."""
    code = _extract_shortcode(url)
    if not code:
        return ExtractOutcome(ok=False, extractor_name=slot_name, error="no_shortcode")
    embed_url = f"https://www.instagram.com/p/{code}/embed/captioned/"
    status, html = await fetch_text(embed_url, headers={"User-Agent": ua})
    if status != 200 or not html:
        return ExtractOutcome(ok=False, extractor_name=slot_name, error=f"http_{status}")

    # Look for cdninstagram / fbcdn URL in the contextJSON
    # The contextJSON has \u002F for / and \u00253D for = — need to unescape
    raw_html = html.replace("\\u002F", "/").replace("\\/", "/").replace("\\u00253D", "=").replace("\\u0025", "%")
    m = re.search(r"https://[^\"' ]+(?:cdninstagram|fbcdn|scontent)[^\"' ]+\.(?:mp4|webp|jpg)", raw_html)
    if not m:
        # Check for contextJSON:null (the wall, when our UA is not allow-listed)
        if "contextJSON:null" in html or html.strip() == "":
            return ExtractOutcome(ok=False, extractor_name=slot_name, error="datacenter_ip_walled")
        return ExtractOutcome(ok=False, extractor_name=slot_name, error="no_media_url")
    media_url = m.group(0).replace("&amp;", "&")
    if not _is_allowed_cdn(media_url):
        return ExtractOutcome(ok=False, extractor_name=slot_name, error="cdn_not_allowed")

    out_path = dest if str(dest).endswith((".mp4", ".jpg", ".webp")) else dest / f"{code}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(media_url, str(out_path), headers={"Referer": "https://www.instagram.com/", "User-Agent": ua})
    if not ok:
        return ExtractOutcome(ok=False, extractor_name=slot_name, error=f"download_{err}")
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", "jpg", "webp", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name=slot_name, error=f"magic_byte_{kind}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(source_platform_post_id=code),
        extractor_name=slot_name,
    )


async def _try_embed_fb(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 2: embed/captioned/ + facebookexternalhit UA (same UA as slot 1 but stdlib only)."""
    return await _try_embed_captioned_with_ua(url, dest, opts, FB_EXTERNAL_HIT_UA, "instagram:embed_fb")


async def _try_embed_android(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 3: embed/captioned/ + Instagram Android-app UA."""
    return await _try_embed_captioned_with_ua(url, dest, opts, ANDROID_IG_UA, "instagram:embed_android")


async def _try_embed_ios(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 4: embed/captioned/ + Instagram iOS-app UA."""
    return await _try_embed_captioned_with_ua(url, dest, opts, IOS_IG_UA, "instagram:embed_ios")


async def _try_mirror(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 5: Third-party mirror (ddinstagram)."""
    code = _extract_shortcode(url)
    if not code:
        return ExtractOutcome(ok=False, extractor_name="instagram:mirror", error="no_shortcode")
    mirror_url = f"https://ddinstagram.com/p/{code}"
    headers = {"User-Agent": "Mozilla/5.0"}
    status, html = await fetch_text(mirror_url, headers=headers)
    if status != 200 or not html:
        return ExtractOutcome(ok=False, extractor_name="instagram:mirror", error=f"http_{status}")
    m = re.search(r"https://[^\"' ]+(?:cdninstagram|fbcdn)[^\"' ]+\.(?:mp4|webp|jpg)", html)
    if not m:
        return ExtractOutcome(ok=False, extractor_name="instagram:mirror", error="no_media_in_mirror")
    media_url = m.group(0).replace("&amp;", "&")
    if not _is_allowed_cdn(media_url):
        return ExtractOutcome(ok=False, extractor_name="instagram:mirror", error="cdn_not_allowed")
    ext = ".mp4" if ".mp4" in media_url.lower() else ".jpg"
    out_path = dest if str(dest).endswith((".mp4", ".jpg", ".webp")) else dest / f"{code}{ext}"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(media_url, str(out_path))
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="instagram:mirror", error=f"download_{err}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(source_platform_post_id=code),
        extractor_name="instagram:mirror",
    )


class InstagramExtractor(BaseExtractor):
    meta = ExtractorMeta(
        name="instagram",
        priority=140,
        url_patterns=INSTAGRAM_URL_PATTERNS,
        platforms=["instagram"],
        description="Instagram fallback chain: yt-dlp+facebookexternalhit UA → embed/captioned/ → embed/android → embed/ios → ddinstagram mirror",
    )

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        opts = opts or {}
        for slot in (_try_ytdlp_facebookexternalhit, _try_embed_fb, _try_embed_android, _try_embed_ios, _try_mirror):
            try:
                outcome = await slot(url, dest, opts)
                if outcome.ok:
                    log.info("instagram_slot_ok", slot=outcome.extractor_name, url=url)
                    return outcome
                log.info("instagram_slot_fail", slot=outcome.extractor_name, error=outcome.error, url=url)
            except Exception as e:
                log.warning("instagram_slot_exception", slot=slot.__name__, error=str(e))
        return ExtractOutcome(
            ok=False,
            extractor_name="instagram:chain",
            error="datacenter_ip_walled",
        )


ExtractorRegistry.register(InstagramExtractor())
