#!/usr/bin/env python3
"""
ttagent — Agentic TikTok video/music harvester for AI agents
=============================================================
One goal: the calling agent gives us a TikTok video URL (or bare video
id); we return a verified, on-disk copy of the video, its soundtrack,
and covers, plus a machine-readable manifest of everything we learned.
Everything else is implementation.

Pipeline (three replaceable tiers, slots not brands):

    DISCOVERY ──► DECODE ──────────► DELIVER
    (normalize    (tikwm │ embed_v2  (CDN fetcher,
     URLs, expand  │      │ tiklydown  magic-byte
     vm/vt links)  │      │ oembed)    verified)

The six non-negotiable constraints (same doctrine as our siblings
`xthread-agent`, `ytagent`, and `igagent`):

  1. No login, no cookies, no OAuth, no browser.  Public content only;
     every slot fails closed when a wall is hit.
  2. No GUI, no interactive prompts. 100% non-interactive CLI.
  3. No LLM at runtime.  A deterministic state machine.
  4. stdlib only.  Single file, zero pip dependencies, Python 3.9+.
  5. Logs on stderr, data on stdout.  Always pipe-safe (`--json`).
  6. Files stay under the output directory.  Media URLs are allowlisted
     to TikTok/mirror CDN hosts; identifiers are validated before they
     ever touch the filesystem.

TikTok-specific doctrine notes:

  * A TikTok post is a *video plus its soundtrack*. The soundtrack is
    media: the music track is harvested alongside the video (opt-out
    with `--no-music`), and its identity (title, author, original?) is
    part of the envelope.
  * Slideshow posts ("photo mode") are first-class: `media.images[]`
    carries the still frames, and the soundtrack is usually the whole
    point.
  * Watermark variants are recorded when exposed (`watermarked_url`)
    but the clean `play_url` / `hd_url` are the primary deliverables.

Slots are interchangeable implementations of a contract.  When one
dies, replace the slot — never restructure the pipeline.  Provenance is
always recorded (`extraction_source`, `decode_slots_tried`).

For the machine-readable operating manual, read `agent.md` in the repo
root. This file is the single source of truth for behavior; the docs
describe it, never the other way around.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"
SCHEMA_VERSION = "1.0"
TOOL_NAME = "ttagent"

# ── Politeness / bounds (constants, not config — see agent.md §2) ───────────
DECODE_SLEEP = 0.6          # seconds between decode-slot attempts
DECODE_TIMEOUT = 30         # per decode-slot HTTP timeout
WALK_TIMEOUT = 40           # short-link expansion timeout
DOWNLOAD_TIMEOUT = 180      # per-file download timeout
DOWNLOAD_TRIES = 3
DECODE_MAX_BYTES = 10 * 1024 * 1024    # decode response cap
DOWNLOAD_CHUNK = 1 * 1024 * 1024       # 1 MB streaming chunks

UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Delivery tier allowlist — https + these host suffixes ONLY.
# tikwm serves proxied media from its own domain; the tiktok/byteoversea
# hosts are the first-party CDNs; akamaized carries ttwvideo streams.
CDN_SUFFIXES = (".tikwm.com", ".tiktokcdn.com", ".tiktokcdn-us.com",
                ".tiktokcdn-eu.com", ".tiktok.com", ".tiktokv.com",
                ".tiktokv.us", ".byteoversea.com", ".ibyteimg.com",
                ".akamaized.net", ".muscdn.com", ".musical.ly",
                ".ttwstatic.com")

MIRROR_HOST = "https://www.tikwm.com"   # prefix for relative media paths

LOG_QUIET = False          # global gate; --quiet flips it
ERRORS: list = []          # shared structured error sink

STAGE_DISCOVERY = "discovery"
STAGE_DECODER = "decoder"
STAGE_FETCHER = "fetcher"
STAGE_ORCHESTRATOR = "orchestrator"

# Stable error codes — the contract consumers branch on.  Never rename.
E_INVALID_INPUT = "E_INVALID_INPUT"
E_SHORTLINK_DEAD = "E_SHORTLINK_DEAD"
E_VIDEO_UNAVAILABLE = "E_VIDEO_UNAVAILABLE"
E_DECODE_FAILED = "E_DECODE_FAILED"
E_DOWNLOAD_FAILED = "E_DOWNLOAD_FAILED"
E_MANIFEST_WRITE_FAILED = "E_MANIFEST_WRITE_FAILED"


# ── Logging & error sink ────────────────────────────────────────────────────
def log(msg: str) -> None:
    """Human-facing diagnostics — stderr only, always."""
    if not LOG_QUIET:
        print(msg, file=sys.stderr)


def record_error(stage: str, code: str, message: str,
                 subject: str = None) -> dict:
    """Append to the shared error sink and echo to stderr."""
    entry = {"stage": stage, "code": code,
             "message": message, "subject": subject}
    ERRORS.append(entry)
    log(f"[err] {stage}: {code}: {message}"
        + (f" ({subject})" if subject else ""))
    return entry


class InputError(Exception):
    """Bad user input with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── Discovery: input normalization ──────────────────────────────────────────
_VIDEO_ID_RE = re.compile(r"^\d{15,20}$")
_TT_VIDEO_RE = re.compile(
    r"^(?:www\.|m\.)?tiktok\.com"
    r"/(?:@[\w.\-]{1,24})?/(?:video|photo)/(\d{15,20})(?:[/?#]|$)",
    re.IGNORECASE)
_SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com")
_T_CODE_RE = re.compile(
    r"^(?:www\.)?tiktok\.com/t/([\w-]{2,32})(?:[/?#]|$)", re.IGNORECASE)


def normalize_input(inp: str) -> dict:
    """Parse one user input into a discovery result.  Raises InputError.

    Accepts:
      * https://www.tiktok.com/@user/video/<id>   (and /photo/<id>)
      * m.tiktok.com variant of the above
      * short links: vm.tiktok.com/<code>, vt.tiktok.com/<code>,
        tiktok.com/t/<code>  — expanded exactly one hop
      * bare video ids (15–20 digits)

    Returns {"input", "video_id", "handle", "short_url", "kind",
             "canonical_url"} with kind ∈ {video, short, bare}.
    canonical_url for bare ids uses the `@i` placeholder handle, which
    the decode slots accept; it is replaced once a real handle is known.
    """
    if not inp or not isinstance(inp, str) or not inp.strip():
        raise InputError(E_INVALID_INPUT, "empty input")
    raw = inp.strip()
    if " " in raw or "\n" in raw or "\t" in raw:
        raise InputError(E_INVALID_INPUT,
                         "input contains whitespace; pass one URL or one id")
    candidate = raw
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/") if (
            "." in candidate or "/" in candidate) else candidate

    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()

    # Bare numeric id
    if _VIDEO_ID_RE.match(raw):
        vid = raw
        return {"input": raw, "video_id": vid, "handle": None,
                "short_url": None, "kind": "bare",
                "canonical_url": f"https://www.tiktok.com/@i/video/{vid}"}

    # @user/video/<id> and /photo/<id>
    m = _TT_VIDEO_RE.match(host + parsed.path)
    if m:
        vid = m.group(1)
        handle = None
        hm = re.match(r"^(?:www\.|m\.)?tiktok\.com/@([\w.\-]{1,24})/",
                      (host + parsed.path), re.IGNORECASE)
        if hm:
            handle = hm.group(1)
        canon = (f"https://www.tiktok.com/@{handle}/video/{vid}"
                 if handle else
                 f"https://www.tiktok.com/@i/video/{vid}")
        return {"input": raw, "video_id": vid, "handle": handle,
                "short_url": None, "kind": "video", "canonical_url": canon}

    # Short links — vm./vt. hosts and /t/<code>
    if host in _SHORT_HOSTS:
        return {"input": raw, "video_id": None, "handle": None,
                "short_url": raw, "kind": "short",
                "canonical_url": raw}
    m = _T_CODE_RE.match(host + parsed.path)
    if m:
        return {"input": raw, "video_id": None, "handle": None,
                "short_url": f"https://www.tiktok.com/t/{m.group(1)}/",
                "kind": "short", "canonical_url": raw}

    if "tiktok.com" in host:
        raise InputError(
            E_INVALID_INPUT,
            f"unsupported tiktok.com path: {parsed.path or '/'} "
            "(expected @user/video/<id>, /photo/<id>, /t/<code> or a "
            "vm./vt. short link)")

    raise InputError(E_INVALID_INPUT,
                     f"not a recognized TikTok URL or video id: {raw!r}")


# ── HTTP primitives ─────────────────────────────────────────────────────────
def http_get(url: str, timeout: float = DECODE_TIMEOUT,
             max_bytes: int = DECODE_MAX_BYTES,
             headers: dict = None) -> bytes:
    """Fetch up to max_bytes.  Raises OSError/urllib.error on failure."""
    req = urllib.request.Request(
        url, headers=headers or {"User-Agent": UA_BROWSER})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes + 1)[:max_bytes]


def _headers(extra: dict = None) -> dict:
    h = {"User-Agent": UA_BROWSER,
         "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    if extra:
        h.update(extra)
    return h


def expand_short_link(url: str) -> str:
    """Expand vm./vt./t/ short links exactly ONE hop.

    We issue a HEAD (falling back to GET) and read the final URL after
    urllib's redirect handling.  Landing anywhere other than a
    @user/video|photo path means the link is dead (expired vanity code
    → TikTok funnels to /about) — an honest E_SHORTLINK_DEAD.
    """
    try:
        req = urllib.request.Request(url, headers=_headers(), method="HEAD")
        try:
            resp = urllib.request.urlopen(req, timeout=WALK_TIMEOUT)
            landed = resp.geturl()
            resp.close()
        except urllib.error.HTTPError as e:
            landed = e.geturl() if hasattr(e, "geturl") else ""
        if not landed:
            raise OSError("no redirect target")
        norm = normalize_input(landed)
        if norm["video_id"] is None:
            raise InputError(
                E_SHORTLINK_DEAD,
                f"short link expanded to a non-video page: {landed} "
                "(expired or deleted)")
        return landed
    except InputError:
        raise
    except Exception as e:  # noqa: BLE001
        raise InputError(
            E_SHORTLINK_DEAD,
            f"could not expand short link (one hop only): "
            f"{type(e).__name__}: {e}")


def _media_url_allowed(url: str) -> bool:
    """Delivery allowlist: https + known TikTok/mirror CDN hosts only."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s)
               for s in CDN_SUFFIXES)


def _abs_media_url(url):
    """Mirror workers return relative media paths — absolutize or pass."""
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return MIRROR_HOST + url
    return url


def _ext_from_url(url: str, default: str = "jpg") -> str:
    path = urllib.parse.urlsplit(url).path or ""
    m = re.search(r"\.(jpe?g|png|webp|heic|mp4|mov|mp3|m4a|gif)$",
                  path, re.IGNORECASE)
    return (m.group(1).lower().replace("jpeg", "jpg") if m else default)


# ── Slot 1: tikwm — the mirror worker API (primary) ─────────────────────────
def _fetch_tikwm(url: str):
    """Slot 1: tikwm.com mirror API.

    Outcomes: ("ok", post) | ("unavailable", None) | ("failed", None).
    The API distinguishes honestly: code 0 with data = success; code -1
    with "Url parsing is failed" = the video does not parse (dead /
    private / regional).  The richest slot: clean + HD + watermarked
    video URLs, covers, music, stats, author, slideshows.
    """
    api = ("https://www.tikwm.com/api/?url="
           + urllib.parse.quote(url, safe="")
           + "&hd=1")
    try:
        body = http_get(api, timeout=DECODE_TIMEOUT,
                        headers=_headers({"Referer": "https://www.tikwm.com/"}))
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "unavailable", None
        return "failed", None
    except (OSError, ValueError):
        return "failed", None

    if not isinstance(payload, dict) or payload.get("code") != 0 \
            or not isinstance(payload.get("data"), dict):
        msg = str(payload.get("msg") or "unknown mirror error")
        if "parsing" in msg.lower() or "deleted" in msg.lower() \
                or "private" in msg.lower():
            return "unavailable", None
        return "failed", None

    d = payload["data"]
    vid = str(d.get("id") or "")
    if not re.fullmatch(r"\d{10,25}", vid):
        return "failed", None

    music_url = _abs_media_url(d.get("music") or
                               (d.get("music_info") or {}).get("play"))
    music_info = d.get("music_info") or {}
    author = d.get("author") or {}
    items = []

    hd = _abs_media_url(d.get("hdplay"))
    play = _abs_media_url(d.get("play"))
    wm = _abs_media_url(d.get("wmplay"))
    video_url = hd or play or wm
    if video_url and not d.get("images"):
        items.append({"type": "video", "url": video_url,
                      "hd_url": hd, "play_url": play,
                      "watermarked_url": wm,
                      "poster_url": _abs_media_url(d.get("origin_cover"))
                      or _abs_media_url(d.get("cover")),
                      "width": None, "height": None,
                      "duration": d.get("duration")})
    for i, img in enumerate((d.get("images") or [])[:50], start=1):
        u = img.get("url") if isinstance(img, dict) else img
        u = _abs_media_url(u)
        if u:
            items.append({"type": "image", "url": u, "slide_index": i})

    post = {
        "video_id": vid,
        "handle": author.get("unique_id"),
        "canonical_url": (f"https://www.tiktok.com/@{author.get('unique_id')}"
                          f"/video/{vid}") if author.get("unique_id") else None,
        "title": d.get("title") or None,
        "region": d.get("region") or None,
        "created_at": d.get("create_time"),
        "duration": d.get("duration"),
        "cover_url": _abs_media_url(d.get("cover")),
        "origin_cover_url": _abs_media_url(d.get("origin_cover")),
        "author": {
            "unique_id": author.get("unique_id"),
            "nickname": author.get("nickname"),
            "avatar_url": _abs_media_url(author.get("avatar")),
            "author_id": str(author.get("id")) if author.get("id") else None,
        },
        "music": {
            "url": music_url,
            "title": music_info.get("title"),
            "author": music_info.get("author"),
            "original": bool(music_info.get("original")) if
            music_info.get("original") is not None else None,
            "duration": music_info.get("duration"),
            "music_id": str(music_info.get("id")) if music_info.get("id")
            else None,
        },
        "stats": {
            "plays": d.get("play_count"),
            "likes": d.get("digg_count"),
            "comments": d.get("comment_count"),
            "shares": d.get("share_count"),
            "collects": d.get("collect_count"),
            "downloads": d.get("download_count"),
        },
        "media_items": items,
        "extraction_source": "tikwm",
    }
    return "ok", post


# ── Slot 2: embed_v2 — TikTok's own public embed page ───────────────────────
def _fetch_embed(vid: str):
    """Slot 2: tiktok.com/embed/v2/<id> JSON mining.

    When TikTok serves the embed with its hydration blob, the full
    itemStruct is present (video URLs, covers, music, author, stats).
    From datacenter IPs this is often a shell — `failed`, fall through.
    """
    url = f"https://www.tiktok.com/embed/v2/{vid}"
    try:
        body = http_get(url, timeout=DECODE_TIMEOUT, headers=_headers())
        html = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "unavailable", None
        return "failed", None
    except (OSError, ValueError):
        return "failed", None

    low = html.lower()
    if "couldn ' t find" in low or "couldn't find" in low or \
            "not available" in low:
        return "unavailable", None

    item = None
    m = re.search(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>'
        r"(.*?)</script>", html, re.S)
    if m:
        try:
            blob = json.loads(m.group(1))
            item = (((blob.get("__DEFAULT_SCOPE__") or {})
                     .get("webapp.video-detail") or {})
                    .get("itemInfo") or {}).get("itemStruct")
        except ValueError:
            item = None
    if item is None:
        m = re.search(r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
                      html, re.S)
        if m:
            try:
                sigi = json.loads(m.group(1))
                item = ((sigi.get("ItemModule") or {}).get(vid))
            except ValueError:
                item = None
    if not isinstance(item, dict):
        return "failed", None

    video = item.get("video") or {}
    music = item.get("music") or {}
    author = item.get("author") or {}
    stats = item.get("stats") or item.get("statsV2") or {}
    play = video.get("playAddr") or video.get("downloadAddr")
    if not play:
        return "failed", None

    def _num(key):
        v = stats.get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    post = {
        "video_id": vid,
        "handle": author.get("uniqueId"),
        "canonical_url": (f"https://www.tiktok.com/@{author.get('uniqueId')}"
                          f"/video/{vid}") if author.get("uniqueId") else None,
        "title": (item.get("desc") or None),
        "region": None,
        "created_at": item.get("createTime"),
        "duration": video.get("duration"),
        "cover_url": video.get("cover"),
        "origin_cover_url": video.get("originCover"),
        "author": {
            "unique_id": author.get("uniqueId"),
            "nickname": author.get("nickname"),
            "avatar_url": author.get("avatarLarger")
            or author.get("avatarMedium"),
            "author_id": str(author.get("id")) if author.get("id") else None,
        },
        "music": {
            "url": music.get("playUrl"),
            "title": music.get("title"),
            "author": music.get("authorName"),
            "original": None,
            "duration": music.get("duration"),
            "music_id": str(music.get("id")) if music.get("id") else None,
        },
        "stats": {
            "plays": _num("playCount"),
            "likes": _num("diggCount"),
            "comments": _num("commentCount"),
            "shares": _num("shareCount"),
            "collects": _num("collectCount"),
            "downloads": None,
        },
        "media_items": [{
            "type": "video", "url": play,
            "hd_url": None, "play_url": play, "watermarked_url":
            video.get("downloadAddr"),
            "poster_url": video.get("originCover") or video.get("cover"),
            "width": video.get("width"), "height": video.get("height"),
            "duration": video.get("duration"),
        }],
        "extraction_source": "embed_v2",
    }
    return "ok", post


# ── Slot 3: tiklydown — community decoder (documented, may be dead) ─────────
def _fetch_tiklydown(url: str):
    """Slot 3: api.tiklydown.eu.org — kept as a slot per the doors
    philosophy; reachability varies.  Fails closed and quietly."""
    api = ("https://api.tiklydown.eu.org/api/download?url="
           + urllib.parse.quote(url, safe=""))
    try:
        body = http_get(api, timeout=DECODE_TIMEOUT, headers=_headers())
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, OSError, ValueError):
        return "failed", None

    data = payload.get("data") if isinstance(payload, dict) else None
    video = (data or {}).get("video") or {}
    vid = str((data or {}).get("id") or "")
    if not re.fullmatch(r"\d{10,25}", vid):
        return "failed", None
    vurl = video.get("noWatermark") or video.get("play") or \
        video.get("watermark")
    if not vurl:
        return "failed", None

    music = (data or {}).get("music") or {}
    author = (data or {}).get("author") or {}
    stats = (data or {}).get("stats") or {}
    return "ok", {
        "video_id": vid,
        "handle": author.get("unique_id"),
        "canonical_url": (f"https://www.tiktok.com/@{author.get('unique_id')}"
                          f"/video/{vid}") if author.get("unique_id") else None,
        "title": (data or {}).get("title") or None,
        "region": None,
        "created_at": None,
        "duration": None,
        "cover_url": video.get("cover") or (data or {}).get("cover"),
        "origin_cover_url": video.get("originCover"),
        "author": {
            "unique_id": author.get("unique_id"),
            "nickname": author.get("nickname"),
            "avatar_url": author.get("avatar"),
            "author_id": str(author.get("id")) if author.get("id") else None,
        },
        "music": {
            "url": music.get("play_url") or music.get("url"),
            "title": music.get("title"),
            "author": music.get("author"),
            "original": None,
            "duration": music.get("duration"),
            "music_id": str(music.get("id")) if music.get("id") else None,
        },
        "stats": {
            "plays": stats.get("playCount"),
            "likes": stats.get("diggCount"),
            "comments": stats.get("commentCount"),
            "shares": stats.get("shareCount"),
            "collects": None,
            "downloads": None,
        },
        "media_items": [{
            "type": "video", "url": vurl,
            "hd_url": video.get("hd") or None,
            "play_url": video.get("noWatermark"),
            "watermarked_url": video.get("watermark"),
            "poster_url": video.get("cover"),
            "width": None, "height": None, "duration": None,
        }],
        "extraction_source": "tiklydown",
    }


# ── Slot 4: oembed — the official, metadata-only last resort ────────────────
def _fetch_oembed(url: str):
    """Slot 4: tiktok.com/oembed — official, no video bytes, ever.

    When every full-content slot is walled, oembed may still describe
    the video: title, author, thumbnail.  The envelope then carries the
    metadata with `downloadable: false, reason: "metadata_only"` — a
    partial success, honestly labeled.
    """
    api = ("https://www.tiktok.com/oembed?url="
           + urllib.parse.quote(url, safe=""))
    try:
        body = http_get(api, timeout=DECODE_TIMEOUT, headers=_headers())
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "unavailable", None
        return "failed", None
    except (OSError, ValueError):
        return "failed", None

    if not isinstance(payload, dict) or not payload.get("title"):
        return "failed", None
    # Try to recover the video id from the embed html/author_url.
    vid = None
    hm = (re.search(r"data-video-id=\"(\d{15,20})\"",
                    str(payload.get("html") or ""))
          or re.search(r"/video/(\d{15,20})",
                       str(payload.get("author_url") or "")))
    if hm:
        vid = hm.group(1)
    return "ok", {
        "video_id": vid,
        "handle": payload.get("author_unique_id") or
        (payload.get("author_url") or "").rstrip("/").split("@")[-1] or None,
        "canonical_url": url,
        "title": payload.get("title"),
        "region": None,
        "created_at": None,
        "duration": None,
        "cover_url": payload.get("thumbnail_url"),
        "origin_cover_url": None,
        "author": {
            "unique_id": payload.get("author_unique_id") or
            (payload.get("author_url") or "").rstrip("/").split("@")[-1]
            or None,
            "nickname": payload.get("author_name"),
            "avatar_url": None,
            "author_id": None,
        },
        "music": {"url": None, "title": None, "author": None,
                  "original": None, "duration": None, "music_id": None},
        "stats": {"plays": None, "likes": None, "comments": None,
                  "shares": None, "collects": None, "downloads": None},
        "media_items": [{
            "type": "video", "url": None,
            "hd_url": None, "play_url": None, "watermarked_url": None,
            "poster_url": payload.get("thumbnail_url"),
            "width": payload.get("width"), "height": payload.get("height"),
            "duration": None,
        }],
        "extraction_source": "oembed",
    }


# ── Decode orchestrator: walk the slots, record provenance ──────────────────
# Slot function table (module constant so tests can patch it).  Slots are
# replaceable implementations of one contract — swap a dead one here.
DECODE_SLOT_FNS = [
    ("tikwm", _fetch_tikwm),
    ("embed_v2", _fetch_embed),
    ("tiklydown", _fetch_tiklydown),
    ("oembed", _fetch_oembed),
]


def decode_video(norm: dict):
    """Run every decode slot in order until one returns "ok".

    Returns (post_dict | None, trace) where trace is the provenance list
    [{"slot", "outcome"}] in try order.  A slot failing over to the next
    is designed behavior — recorded in the trace, not in the error sink.
    """
    trace = []
    unavailable_seen = False
    target = norm["short_url"] or norm["canonical_url"]
    for name, fn in DECODE_SLOT_FNS:
        try:
            if name == "embed_v2":
                # embed slot keys off the bare id
                outcome, post = fn(norm["video_id"])
            else:
                outcome, post = fn(target)
        except Exception as e:  # noqa: BLE001 — a slot must never crash the run
            log(f"[warn] decoder slot {name} raised: "
                f"{type(e).__name__}: {e}")
            trace.append({"slot": name, "outcome": f"crashed: {type(e).__name__}"})
            time.sleep(DECODE_SLEEP)
            continue
        trace.append({"slot": name, "outcome": outcome})
        if outcome == "ok" and post:
            log(f"[ok ] decoded via slot '{name}'")
            return post, trace
        if outcome == "unavailable":
            unavailable_seen = True
        else:
            log(f"[..] slot {name}: no usable payload; falling through")
        time.sleep(DECODE_SLEEP)
    if unavailable_seen:
        record_error(STAGE_DECODER, E_VIDEO_UNAVAILABLE,
                     f"all {len(trace)} decode slots report the video "
                     "unavailable", subject=norm["video_id"])
    else:
        record_error(STAGE_DECODER, E_DECODE_FAILED,
                     f"all {len(trace)} decode slots failed to produce "
                     "a payload", subject=norm["video_id"])
    return None, trace


# ── Delivery: verified downloads (never trust a self-report) ────────────────
def _magic_kind(head: bytes):
    """Identify a file by magic bytes — the only verification that counts."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF
                              and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] in (b"GIF8",):
        return "gif"
    return None


def download(url: str, dest: Path, tries: int = DOWNLOAD_TRIES) -> bool:
    """Stream one CDN file to dest with atomic replace + magic-byte verify.

    Contract (same doctrine as the siblings):
      * allowlisted https CDN URLs only
      * write to `<dest>.part`, verify, then os.replace — a reader never
        sees a half-written file
      * verify Content-Length when the server sends one
      * verify magic bytes; a file that claims to be media and is not
        gets deleted, and failure is recorded — never faked
    """
    if not _media_url_allowed(url):
        record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                     "URL rejected by delivery allowlist", subject=url)
        return False
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA_BROWSER, "Accept": "*/*",
                "Referer": "https://www.tiktok.com/"})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                expected = resp.headers.get("Content-Length")
                tmp = dest.with_suffix(dest.suffix + ".part")
                received = 0
                with open(tmp, "wb") as fh:
                    head = b""
                    while True:
                        chunk = resp.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        if not head:
                            head = chunk[:16]
                        fh.write(chunk)
                        received += len(chunk)
            if expected and received != int(expected):
                record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                             f"size mismatch: got {received}, "
                             f"expected {expected}", subject=dest.name)
                tmp.unlink(missing_ok=True)
                continue
            if received == 0:
                record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                             "0 bytes received", subject=dest.name)
                tmp.unlink(missing_ok=True)
                continue
            if _magic_kind(head[:16]) is None:
                record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                             f"magic-byte verification failed "
                             f"(head={head[:8].hex()})", subject=dest.name)
                tmp.unlink(missing_ok=True)
                return False   # not a network flake — do not retry
            os.replace(tmp, dest)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                             f"HTTP {e.code} from CDN", subject=dest.name)
                return False
            if attempt < tries:
                time.sleep(1.5 * attempt)
        except (OSError, ValueError) as e:
            if attempt < tries:
                time.sleep(1.5 * attempt)
            else:
                record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                             f"{type(e).__name__}: {e}", subject=dest.name)
    return False


# ── Orchestration: harvest ──────────────────────────────────────────────────
def _iso_from_ts(ts):
    """Unix seconds → ISO-8601 UTC string, or None (honest nulls)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _safe_video_id(value: str):
    if not value or not re.fullmatch(r"\d{10,25}", value):
        return None
    return value


def harvest(norm: dict, out: Path, do_download: bool = True,
            do_music: bool = True) -> dict:
    """Full pipeline: decode → map → (download) → envelope.  Never raises.

    The envelope is the single source of truth for the caller; everything
    material that happened is in it — statuses, provenance, errors, counts.
    """
    started = time.monotonic()
    vid = norm["video_id"]
    out.mkdir(parents=True, exist_ok=True)

    post, trace = decode_video(norm)
    if post is None:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "source": {"tool": TOOL_NAME, "version": __version__,
                       "generated_at": datetime.now(timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ")},
            "request": {"input": norm["input"], "video_id": vid,
                        "canonical_url": norm["canonical_url"],
                        "options": {"download_media": bool(do_download),
                                    "download_music": bool(do_music)}},
            "status": "empty", "video": None, "errors": list(ERRORS),
            "metadata": {"duration_sec": round(time.monotonic() - started, 3),
                         "decode_slots_tried": trace,
                         "counts": {"videos": 0, "images": 0,
                                    "audio_tracks": 0, "downloaded_media": 0,
                                    "failed_downloads": 0}},
        }
        _write_manifest(out, envelope)
        return envelope

    # Canonicalize: a slot may have recovered the real handle.
    canonical = post.get("canonical_url") or norm["canonical_url"]
    video_id = _safe_video_id(post.get("video_id") or vid) or vid

    items = post.get("media_items") or []
    video_item = next((i for i in items if i.get("type") == "video"), None)
    image_items = [i for i in items if i.get("type") == "image"]

    downloaded = 0
    failed = 0
    media = {"video": None, "covers": {"cover_url": post.get("cover_url"),
                                       "cover_file": None,
                                       "cover_downloaded": False,
                                       "origin_cover_url":
                                       post.get("origin_cover_url"),
                                       "origin_cover_file": None,
                                       "origin_cover_downloaded": False},
             "images": []}

    # ── the video (primary deliverable) ──
    if video_item is not None:
        url = video_item.get("url")
        hd_url = video_item.get("hd_url")
        wm_url = video_item.get("watermarked_url")
        entry = {"url": url, "hd_url": hd_url if hd_url != url else None,
                 "watermarked_url": wm_url if wm_url != url else None,
                 "width": video_item.get("width"),
                 "height": video_item.get("height"),
                 "duration": video_item.get("duration"),
                 "file": None, "hd_file": None, "watermarked_file": None,
                 "downloaded": False,
                 "downloadable": bool(url),
                 "reason": None if url else "no_video_url_exposed"}
        if do_download:
            if url and _media_url_allowed(url):
                ext = _ext_from_url(url, "mp4")
                fname = f"{video_id}_video.{ 'mp4' if ext in ('mp4','mov') else ext }"
                okd = download(url, out / fname)
                entry["downloaded"] = okd
                entry["file"] = fname if okd else None
                if not okd:
                    entry["reason"] = entry["reason"] or "download_failed"
                downloaded += int(okd)
                failed += int(not okd)
            elif url:
                entry["downloadable"] = False
                entry["reason"] = "url_outside_delivery_allowlist"
            if hd_url and hd_url != url and _media_url_allowed(hd_url) \
                    and entry["downloadable"]:
                okh = download(hd_url, out / f"{video_id}_hd.mp4")
                entry["hd_file"] = f"{video_id}_hd.mp4" if okh else None
                if not okh:
                    record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                                 "HD variant download failed",
                                 subject=f"{video_id}_hd.mp4")
                downloaded += int(okh)
                failed += int(not okh)
        media["video"] = entry

        poster = video_item.get("poster_url")
        if do_download and poster and _media_url_allowed(poster):
            okp = download(poster, out / f"{video_id}_cover.jpg")
            if okp:
                media["covers"]["cover_file"] = f"{video_id}_cover.jpg"
                media["covers"]["cover_downloaded"] = True
                downloaded += 1
            else:
                record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                             "cover download failed",
                             subject=f"{video_id}_cover.jpg")
                failed += 1

    # ── the soundtrack (a TikTok post is video + music) ──
    music = post.get("music") or {}
    music_entry = {"url": music.get("url"), "title": music.get("title"),
                   "author": music.get("author"),
                   "original": music.get("original"),
                   "duration": music.get("duration"),
                   "music_id": music.get("music_id"),
                   "file": None, "downloaded": False,
                   "downloadable": bool(music.get("url")), "reason": None}
    if do_music and do_download and music.get("url") \
            and _media_url_allowed(music["url"]):
        okm = download(music["url"], out / f"{video_id}_music.mp3")
        music_entry["downloaded"] = okm
        music_entry["file"] = f"{video_id}_music.mp3" if okm else None
        if not okm:
            music_entry["reason"] = "download_failed"
        downloaded += int(okm)
        failed += int(not okm)
    elif music.get("url") and not _media_url_allowed(music["url"]):
        music_entry["downloadable"] = False
        music_entry["reason"] = "url_outside_delivery_allowlist"
    media["music"] = music_entry

    # ── slideshow images (photo-mode posts) ──
    for item in image_items:
        idx = item.get("slide_index") or (len(media["images"]) + 1)
        url = item.get("url")
        entry = {"url": url, "slide_index": idx, "file": None,
                 "downloaded": False, "downloadable": bool(url),
                 "reason": None}
        if do_download and url and _media_url_allowed(url):
            fname = f"{video_id}_img{idx}.jpg"
            okd = download(url, out / fname)
            entry["downloaded"] = okd
            entry["file"] = fname if okd else None
            if not okd:
                entry["reason"] = "download_failed"
            downloaded += int(okd)
            failed += int(not okd)
        elif url and not _media_url_allowed(url):
            entry["downloadable"] = False
            entry["reason"] = "url_outside_delivery_allowlist"
        media["images"].append(entry)

    status = "ok"
    if ERRORS:
        status = "partial"
    if video_item is not None and not video_item.get("url"):
        status = "partial"   # metadata decoded, no bytes available

    envelope_video = {
        "video_id": video_id,
        "url": canonical,
        "title": post.get("title"),
        "region": post.get("region"),
        "created_at": _iso_from_ts(post.get("created_at")),
        "duration": post.get("duration"),
        "author": post.get("author") or {"unique_id": None,
                                         "nickname": None,
                                         "avatar_url": None,
                                         "author_id": None},
        "extraction_source": post.get("extraction_source"),
        "media": media,
        "stats": post.get("stats") or {"plays": None, "likes": None,
                                       "comments": None, "shares": None,
                                       "collects": None, "downloads": None},
    }

    counts = {
        "videos": 1 if media["video"] else 0,
        "images": len(media["images"]),
        "audio_tracks": 1 if music_entry.get("url") else 0,
        "downloaded_media": downloaded,
        "failed_downloads": failed,
    }

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "source": {"tool": TOOL_NAME, "version": __version__,
                   "generated_at": datetime.now(timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ")},
        "request": {"input": norm["input"], "video_id": video_id,
                    "canonical_url": canonical,
                    "options": {"download_media": bool(do_download),
                                "download_music": bool(do_music)}},
        "status": status,
        "video": envelope_video,
        "errors": list(ERRORS),
        "metadata": {"duration_sec": round(time.monotonic() - started, 3),
                     "decode_slots_tried": trace, "counts": counts},
    }
    _write_manifest(out, envelope)
    return envelope


def _write_manifest(out: Path, envelope: dict) -> bool:
    """Atomic manifest write (`.part` + os.replace, UTF-8, stable key order)."""
    path = out / "video_manifest.json"
    try:
        tmp = path.with_suffix(".json.part")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        log(f"[ok ] manifest written: {path}")
        return True
    except OSError as e:
        record_error(STAGE_ORCHESTRATOR, E_MANIFEST_WRITE_FAILED,
                     f"{type(e).__name__}: {e}", subject=str(path))
        return False


def _summary(envelope: dict, out: Path) -> dict:
    """The pipe-safe stdout summary (one JSON object, `--json`)."""
    video = envelope.get("video") or {}
    counts = (envelope.get("metadata") or {}).get("counts") or {}
    media = video.get("media") or {}
    vid_media = media.get("video") or {}
    return {
        "ok": envelope.get("status") in ("ok", "partial"),
        "status": envelope.get("status"),
        "video_id": (envelope.get("request") or {}).get("video_id"),
        "canonical_url": (envelope.get("request") or {}).get("canonical_url"),
        "extraction_source": video.get("extraction_source"),
        "author": (video.get("author") or {}).get("unique_id"),
        "title": (video.get("title") or "")[:80] or None,
        "video_file": vid_media.get("file"),
        "music_file": (media.get("music") or {}).get("file"),
        "images": counts.get("images", 0),
        "downloaded": counts.get("downloaded_media", 0),
        "failed_downloads": counts.get("failed_downloads", 0),
        "out_dir": str(out),
        "manifest_path": str(out / "video_manifest.json"),
        "errors": envelope.get("errors", []),
        "duration_sec": (envelope.get("metadata") or {}).get("duration_sec"),
    }


def main(argv=None) -> int:
    """CLI entry.  Exit 0 = harvested (ok/partial), 1 = nothing/error,
    2 = usage.  `--json` prints exactly one summary object on stdout."""
    ap = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Harvest a public TikTok video, soundtrack and covers: "
                    "no login, no API keys, no browser, no LLM.")
    ap.add_argument("input", help="TikTok video/photo URL, vm./vt./t/ short "
                                  "link, or bare video id")
    ap.add_argument("--out", default="tiktok_media",
                    help="output directory (default: tiktok_media)")
    ap.add_argument("--no-download", action="store_true",
                    help="metadata only; do not fetch media files")
    ap.add_argument("--no-music", action="store_true",
                    help="skip the soundtrack download")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON summary object on stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="silence stderr diagnostics")
    ap.add_argument("--version", action="version",
                    version=f"{TOOL_NAME} {__version__}")
    args = ap.parse_args(argv)

    global LOG_QUIET
    LOG_QUIET = bool(args.quiet)
    if args.json:
        LOG_QUIET = True

    started = time.monotonic()
    try:
        norm = normalize_input(args.input)
    except InputError as e:
        if args.json:
            print(json.dumps({"ok": False, "status": "invalid_input",
                              "error": {"code": e.code,
                                        "message": e.message}}))
        else:
            log(f"[err] invalid input: {e.message}")
        return 2

    if norm["kind"] == "short":
        try:
            landed = expand_short_link(norm["short_url"])
            norm = normalize_input(landed)
            log(f"[ok ] short link expanded -> {norm['canonical_url']}")
        except InputError as e:
            record_error(STAGE_DISCOVERY, e.code, e.message,
                         subject=norm["short_url"])
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "source": {"tool": TOOL_NAME, "version": __version__,
                           "generated_at": datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")},
                "request": {"input": args.input, "video_id": None,
                            "canonical_url": norm["canonical_url"],
                            "options": {"download_media": not args.no_download,
                                        "download_music": not args.no_music}},
                "status": "empty", "video": None, "errors": list(ERRORS),
                "metadata": {"duration_sec":
                             round(time.monotonic() - started, 3),
                             "decode_slots_tried": [], "counts": {}}}
            out = Path(args.out)
            _write_manifest(out, envelope)
            if args.json:
                print(json.dumps(_summary(envelope, out)))
            return 1

    log(f"[..] harvesting {norm['canonical_url']}")
    out = Path(args.out)
    envelope = harvest(norm, out, do_download=not args.no_download,
                       do_music=not args.no_music)

    summary = _summary(envelope, out)
    ok = envelope["status"] in ("ok", "partial")
    log(f"[{'done' if ok else 'err'}] status={envelope['status']} "
        f"video={bool(summary.get('video_file'))} "
        f"music={bool(summary.get('music_file'))} "
        f"downloaded={summary.get('downloaded', 0)}")
    if args.json:
        print(json.dumps(summary))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
