"""Twitter / X.com extractor — fallback chain verified live 2026-10-03.

Chain (priority order):
  1. api.fxtwitter.com/status/<id>      (primary — verified live)
  2. cdn.syndication.twimg.com/tweet-result  (sanity probe slot)
  3. unrollnow.com thread walker         (for threads)
  4. api.vxtwitter.com                   (last resort, often CF-challenged)
"""
from __future__ import annotations

import re
from pathlib import Path

from avd.extractors.base import BaseExtractor
from avd.extractors.registry import ExtractorRegistry
from avd.models import DownloadMetadata, ExtractOutcome, ExtractorMeta
from avd.utils.breaker import is_open, record_failure, record_success
from avd.utils.fs import detect_file_type, ensure_dir, safe_unlink
from avd.utils.http import fetch_json, stream_download
from avd.utils.logging import get_logger

log = get_logger("avd.extractors.twitter")

TWITTER_URL_PATTERNS = [
    r"^https?://(?:www\.)?(twitter|x)\.com/.+",
    r"^https?://t\.co/.+",
    r"^https?://api\.fxtwitter\.com/.+",
]

TWITTER_CDN_ALLOWLIST = (
    "video.twimg.com",
    "pbs.twimg.com",
    "twimg.com",
    "fxtwitter.com",
    "syndication.twimg.com",
)


def _is_allowed_cdn(url: str) -> bool:
    low = url.lower()
    return any(host in low for host in TWITTER_CDN_ALLOWLIST)


def _extract_tweet_id(url: str) -> str | None:
    """Extract tweet ID from a twitter/x URL.

    Accepts any length digit string — old tweets (jack's first tweet = ID 20)
    and modern tweet IDs (19 digits) both work.
    """
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{10,})(?:\?|$|/)", url)
    if m:
        return m.group(1)
    return None


async def _try_fxtwitter(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 1: api.fxtwitter.com (FixTweet)."""
    tid = _extract_tweet_id(url)
    if not tid:
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="no_tweet_id")
    api = f"https://api.fxtwitter.com/status/{tid}"
    host = "api.fxtwitter.com"
    if is_open(host):
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="circuit_open")
    status, data, raw = await fetch_json(api, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error=f"http_{status}", raw_info={"raw": raw[:500] if raw else None})

    # 404 comes back as HTTP 200 + body {"code":404,"tweet":null} OR real HTTP 404
    if data.get("code") == 404 or data.get("tweet") is None:
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="tweet_not_found")

    tweet = data.get("tweet") or {}
    media = tweet.get("media") or {}
    videos = media.get("videos") or []
    photos = media.get("photos") or []

    if not videos:
        # Image-only tweet — pick highest-quality photo
        if photos:
            photo_url = photos[-1].get("url") if isinstance(photos[-1], dict) else photos[-1]
            if not _is_allowed_cdn(photo_url):
                return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="cdn_not_allowed")
            out_path = dest if str(dest).endswith((".jpg", ".png", ".webp")) else dest / f"{tid}.jpg"
            ensure_dir(out_path.parent)
            ok, _, err = await stream_download(photo_url, str(out_path))
            if not ok:
                record_failure(host)
                return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error=f"download_{err}")
            record_success(host)
            return ExtractOutcome(
                ok=True,
                artifact_path=Path(out_path),
                metadata=DownloadMetadata(
                    source_platform_post_id=tid,
                    title=tweet.get("text"),
                    author=(tweet.get("author") or {}).get("screen_name"),
                    author_id=str((tweet.get("author") or {}).get("id") or ""),
                    thumbnail_url=photos[0].get("url") if photos else None,
                    media_count=len(photos),
                ),
                extractor_name="twitter:fxtwitter",
                raw_info=data,
            )
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="no_media_in_tweet")

    # Pick highest-bitrate mp4 variant
    best = videos[0]
    best_url = best.get("url")
    formats = best.get("formats") or []
    if formats:
        # Sort by bitrate desc
        formats_sorted = sorted(formats, key=lambda f: f.get("bitrate", 0), reverse=True)
        best_url = formats_sorted[0].get("url") or best_url
    if not best_url:
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="no_video_url")
    if not _is_allowed_cdn(best_url):
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error="cdn_not_allowed")

    out_path = dest if str(dest).endswith(".mp4") else dest / f"{tid}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(best_url, str(out_path), headers={"Referer": "https://x.com/"})
    if not ok:
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error=f"download_{err}")
    record_success(host)
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="twitter:fxtwitter", error=f"magic_byte_{kind}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(
            source_platform_post_id=tid,
            title=tweet.get("text"),
            author=(tweet.get("author") or {}).get("screen_name"),
            author_id=str((tweet.get("author") or {}).get("id") or ""),
            duration_s=best.get("duration") or None,
            thumbnail_url=photos[0].get("url") if photos else None,
            media_count=len(videos) + len(photos),
        ),
        extractor_name="twitter:fxtwitter",
        raw_info=data,
    )


async def _try_syndication(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 2: cdn.syndication.twimg.com tweet-result (sanity probe)."""
    tid = _extract_tweet_id(url)
    if not tid:
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error="no_tweet_id")
    api = f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=x"
    host = "cdn.syndication.twimg.com"
    if is_open(host):
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error="circuit_open")
    status, data, _ = await fetch_json(api, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        record_failure(host)
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error=f"http_{status}")
    record_success(host)
    # Pull first video URL
    media = (data.get("media") or {})
    videos = media.get("videos") or (data.get("videos") or [])
    if not videos:
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error="no_video_url")
    # Different shape than fxtwitter — try variants
    v = videos[0]
    vid_url = None
    if isinstance(v, dict):
        vid_url = v.get("video_info", {}).get("variants", [{}])[-1].get("url") if v.get("video_info") else v.get("url")
    if not vid_url:
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error="no_video_url")
    if not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error="cdn_not_allowed")
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{tid}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(vid_url, str(out_path))
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error=f"download_{err}")
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="twitter:syndication", error=f"magic_byte_{kind}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(
            source_platform_post_id=tid,
            title=data.get("text"),
            author=(data.get("user") or {}).get("screen_name"),
        ),
        extractor_name="twitter:syndication",
        raw_info=data,
    )


async def _try_unrollnow(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 3: unrollnow thread walker (best-effort)."""
    # For a single tweet, this just returns the tweet's own ID — useful as a fallback decoder
    tid = _extract_tweet_id(url)
    if not tid:
        return ExtractOutcome(ok=False, extractor_name="twitter:unrollnow", error="no_tweet_id")
    from avd.utils.http import fetch_text
    walker_url = f"https://unrollnow.com/status/{tid}"
    status, html = await fetch_text(walker_url, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200:
        return ExtractOutcome(ok=False, extractor_name="twitter:unrollnow", error=f"http_{status}")
    # Try to extract a video.twimg.com URL
    m = re.search(r"https://video\.twimg\.com/[^\"' <]+\.mp4", html)
    if not m:
        return ExtractOutcome(ok=False, extractor_name="twitter:unrollnow", error="no_video_in_html")
    vid_url = m.group(0).replace("&amp;", "&")
    if not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="twitter:unrollnow", error="cdn_not_allowed")
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{tid}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(vid_url, str(out_path), headers={"Referer": "https://x.com/"})
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="twitter:unrollnow", error=f"download_{err}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(source_platform_post_id=tid),
        extractor_name="twitter:unrollnow",
    )


async def _try_vxtwitter(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 4: api.vxtwitter.com — often Cloudflare-challenged from datacenter IPs."""
    tid = _extract_tweet_id(url)
    if not tid:
        return ExtractOutcome(ok=False, extractor_name="twitter:vxtwitter", error="no_tweet_id")
    api = f"https://api.vxtwitter.com/Twitter/status/{tid}"
    status, data, raw = await fetch_json(api, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        # Likely Cloudflare challenge page — non-JSON
        if "<html" in (raw or "").lower()[:200]:
            return ExtractOutcome(ok=False, extractor_name="twitter:vxtwitter", error="cloudflare_challenge")
        return ExtractOutcome(ok=False, extractor_name="twitter:vxtwitter", error=f"http_{status}")
    media = data.get("media") or {}
    videos = media.get("videos") or []
    if not videos:
        return ExtractOutcome(ok=False, extractor_name="twitter:vxtwitter", error="no_video_url")
    vid_url = videos[0].get("url")
    if not vid_url or not _is_allowed_cdn(vid_url):
        return ExtractOutcome(ok=False, extractor_name="twitter:vxtwitter", error="cdn_not_allowed")
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{tid}.mp4"
    ensure_dir(out_path.parent)
    ok, _, err = await stream_download(vid_url, str(out_path))
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="twitter:vxtwitter", error=f"download_{err}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(
            source_platform_post_id=tid,
            title=data.get("text"),
            author=(data.get("user") or {}).get("screen_name"),
        ),
        extractor_name="twitter:vxtwitter",
        raw_info=data,
    )


class TwitterExtractor(BaseExtractor):
    meta = ExtractorMeta(
        name="twitter",
        priority=110,
        url_patterns=TWITTER_URL_PATTERNS,
        platforms=["twitter"],
        description="Twitter/X fallback chain: fxtwitter → syndication → unrollnow → vxtwitter",
    )

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        opts = opts or {}
        # Expand t.co short links
        if "t.co/" in url:
            from avd.utils.http import head_url
            expanded = await head_url(url)
            if expanded:
                url = expanded
        for slot in (_try_fxtwitter, _try_syndication, _try_unrollnow, _try_vxtwitter):
            try:
                outcome = await slot(url, dest, opts)
                if outcome.ok:
                    log.info("twitter_slot_ok", slot=outcome.extractor_name, url=url)
                    return outcome
                log.info("twitter_slot_fail", slot=outcome.extractor_name, error=outcome.error, url=url)
            except Exception as e:
                log.warning("twitter_slot_exception", slot=slot.__name__, error=str(e))
        return ExtractOutcome(ok=False, extractor_name="twitter:chain", error="all_slots_failed")


ExtractorRegistry.register(TwitterExtractor())
