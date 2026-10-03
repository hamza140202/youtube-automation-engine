"""Truth Agent — cross-references downloaded metadata against source platform."""
from __future__ import annotations

import re
from typing import Any

from avd.models import TruthReport
from avd.utils.http import fetch_json, fetch_text
from avd.utils.logging import get_logger

try:
    from Levenshtein import ratio as lev_ratio
except ImportError:  # pragma: no cover
    def lev_ratio(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        a, b = a.lower(), b.lower()
        if a == b:
            return 1.0
        # crude fallback
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()


log = get_logger("avd.truth")


def _detect_platform(url: str) -> str:
    if "tiktok.com" in url or "vm.tiktok" in url or "vt.tiktok" in url:
        return "tiktok"
    if "twitter.com" in url or "x.com" in url or "t.co" in url:
        return "twitter"
    if "reddit.com" in url or "redd.it" in url:
        return "reddit"
    if "instagram.com" in url or "instagr.am" in url:
        return "instagram"
    if "xiaohongshu.com" in url or "xhslink.com" in url:
        return "rednote"
    if "douyin.com" in url:
        return "douyin"
    return "unknown"


def _extract_tiktok_id(url: str) -> str | None:
    m = re.search(r"/video/(\d{10,})", url)
    return m.group(1) if m else None


def _extract_tweet_id(url: str) -> str | None:
    m = re.search(r"/status(?:es)?/(\d+)", url)
    return m.group(1) if m else None


async def _fetch_tiktok(url: str) -> dict[str, Any]:
    """TikTok oEmbed — metadata only, no video."""
    api = f"https://www.tiktok.com/oembed?url={url}"
    status, data, _ = await fetch_json(api, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    if data.get("title"):
        out["title"] = data["title"]
    if data.get("author_name"):
        out["author"] = data["author_name"]
    if data.get("thumbnail_url"):
        out["thumbnail_url"] = data["thumbnail_url"]
    return out


async def _fetch_twitter(url: str) -> dict[str, Any]:
    """Twitter via cdn.syndication.twimg.com (sanity probe — independent of fxtwitter)."""
    tid = _extract_tweet_id(url)
    if not tid:
        return {}
    api = f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=x"
    status, data, _ = await fetch_json(api, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200 or not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    if data.get("text"):
        out["title"] = data["text"]
    if (data.get("user") or {}).get("screen_name"):
        out["author"] = data["user"]["screen_name"]
    if (data.get("user") or {}).get("id_str"):
        out["author_id"] = data["user"]["id_str"]
    return out


async def _fetch_reddit(url: str) -> dict[str, Any]:
    """Reddit via RSS feed of the post's permalink (sub-level only)."""
    # Best-effort — fetch the post page's HTML and try to extract title meta tags
    status, html = await fetch_text(url, headers={"User-Agent": "python:avd:1.0.0"})
    if status != 200:
        return {}
    out: dict[str, Any] = {}
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        out["title"] = m.group(1).strip()
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if m:
        out["title"] = m.group(1)
    return out


async def _fetch_instagram(url: str) -> dict[str, Any]:
    """Instagram via OG tags with facebookexternalhit UA."""
    headers = {"User-Agent": "facebookexternalhit/1.1"}
    status, html = await fetch_text(url, headers=headers)
    if status != 200 or not html:
        return {}
    out: dict[str, Any] = {}
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if m:
        out["title"] = m.group(1)
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if m:
        out["thumbnail_url"] = m.group(1)
    return out


async def _fetch_rednote(url: str) -> dict[str, Any]:
    """Rednote via HTML scrape + JSON-LD."""
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    status, html = await fetch_text(url, headers=headers)
    if status != 200:
        return {}
    out: dict[str, Any] = {}
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if m:
        out["title"] = m.group(1)
    return out


async def _fetch_douyin(url: str) -> dict[str, Any]:
    """Douyin via og: tags — low confidence."""
    status, html = await fetch_text(url, headers={"User-Agent": "Mozilla/5.0"})
    if status != 200:
        return {}
    out: dict[str, Any] = {}
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if m:
        out["title"] = m.group(1)
    return out


_FETCHERS = {
    "tiktok": _fetch_tiktok,
    "twitter": _fetch_twitter,
    "reddit": _fetch_reddit,
    "instagram": _fetch_instagram,
    "rednote": _fetch_rednote,
    "douyin": _fetch_douyin,
}


def _compare(source: dict[str, Any], downloaded: dict[str, Any]) -> tuple[dict[str, bool], float, str]:
    """Run field-by-field comparison. Returns (matches, confidence, verdict)."""
    matches: dict[str, bool] = {}
    fields_checked = 0
    fields_matched = 0

    if "title" in source and source["title"] and downloaded.get("title"):
        fields_checked += 1
        ratio = lev_ratio(str(source["title"]), str(downloaded["title"]))
        m = ratio >= 0.7
        matches["title"] = m
        if m:
            fields_matched += 1

    if "author" in source and source["author"] and downloaded.get("author"):
        fields_checked += 1
        m = str(source["author"]).lower().strip() == str(downloaded["author"]).lower().strip()
        matches["author"] = m
        if m:
            fields_matched += 1

    if "author_id" in source and source["author_id"] and downloaded.get("author_id"):
        fields_checked += 1
        m = str(source["author_id"]) == str(downloaded["author_id"])
        matches["author_id"] = m
        if m:
            fields_matched += 1

    if source.get("duration_s") and downloaded.get("duration_s"):
        fields_checked += 1
        m = abs(float(source["duration_s"]) - float(downloaded["duration_s"])) <= 2.0
        matches["duration_s"] = m
        if m:
            fields_matched += 1

    if source.get("media_count") and downloaded.get("media_count"):
        fields_checked += 1
        m = int(source["media_count"]) == int(downloaded["media_count"])
        matches["media_count"] = m
        if m:
            fields_matched += 1

    if fields_checked == 0:
        return matches, 0.0, "unverifiable"
    confidence = fields_matched / fields_checked
    verdict = "verified" if (fields_checked >= 1 and fields_matched == fields_checked) else "suspicious"
    return matches, confidence, verdict


class TruthAgent:
    """Cross-check downloaded metadata against source platform."""

    __version__ = "1.0.0"

    async def cross_check(self, url: str, downloaded_meta: dict) -> TruthReport:
        platform = _detect_platform(url)
        fetcher = _FETCHERS.get(platform)
        if not fetcher:
            return TruthReport(
                url=url,
                platform=platform,
                downloaded_meta=downloaded_meta,
                verdict="unverifiable",
            )
        try:
            source_meta = await fetcher(url)
        except Exception as e:
            log.warning("truth_fetch_failed", platform=platform, error=str(e))
            source_meta = {}
        if not source_meta:
            return TruthReport(
                url=url,
                platform=platform,
                downloaded_meta=downloaded_meta,
                verdict="unverifiable",
            )
        matches, confidence, verdict = _compare(source_meta, downloaded_meta)
        return TruthReport(
            url=url,
            platform=platform,
            source_meta=source_meta,
            downloaded_meta=downloaded_meta,
            matches=matches,
            confidence=confidence,
            verdict=verdict,
        )
