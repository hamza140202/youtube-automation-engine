#!/usr/bin/env python3
"""
igagent — Agentic Instagram post/carousel/reel harvester for AI agents
======================================================================
One goal: the calling agent gives us an Instagram post URL (or bare
shortcode); we return a verified, on-disk copy of its public media plus a
machine-readable manifest of everything we learned. Everything else is
implementation.

Pipeline (three replaceable tiers, slots not brands):

    DISCOVERY ──► DECODE ──► DELIVER
    (normalize    (embed_json  (CDN fetcher,
     shortcode,    │ embed_html │ magic-byte
     share links)  │ og_meta)   │ verified)

The six non-negotiable constraints (same doctrine as our siblings
`xthread-agent` and `ytagent`):

  1. No login, no cookies, no OAuth, no browser.  Public content only;
     every slot fails closed when a wall is hit.
  2. No GUI, no interactive prompts. 100% non-interactive CLI.
  3. No LLM at runtime.  A deterministic state machine.
  4. stdlib only.  Single file, zero pip dependencies, Python 3.9+.
  5. Logs on stderr, data on stdout.  Always pipe-safe (`--json`).
  6. Files stay under the output directory.  Media URLs are allowlisted
     to Instagram CDN hosts; identifiers are validated before they ever
     touch the filesystem.

Slots are interchangeable implementations of a contract.  When one dies,
replace the slot — never restructure the pipeline.  Provenance is always
recorded (`extraction_source`, `decode_slots_tried`).

Design doctrine inherited from the sibling projects:
  * Honesty over completeness — `status: partial`, structured `errors[]`,
    explicit nulls.  Never fabricate a value.
  * Politeness is a hard constraint — bounded retries, sleeps between
    decode calls, response-size caps.  "Restraint is the rent."
  * Never trust a method's self-report — every downloaded file is
    verified against magic bytes before it is allowed to exist.

For the machine-readable operating manual, read `agent.md` in the repo
root. This file is the single source of truth for behavior; the docs
describe it, never the other way around.
"""
from __future__ import annotations

import argparse
import base64
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
TOOL_NAME = "igagent"

# ── Politeness / bounds (constants, not config — see agent.md §2) ───────────
DECODE_SLEEP = 0.6          # seconds between decode-slot attempts
DECODE_TIMEOUT = 30         # per decode-slot HTTP timeout
WALK_TIMEOUT = 40           # discovery (share-link expansion) timeout
DOWNLOAD_TIMEOUT = 180      # per-file download timeout
DOWNLOAD_TRIES = 3
EMBED_MAX_BYTES = 15 * 1024 * 1024     # embed/og response cap
DOWNLOAD_CHUNK = 1 * 1024 * 1024       # 1 MB streaming chunks
MAX_CAROUSEL_CHILDREN = 20            # hard cap on carousel children

UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
UA_UNFURLER = ("facebookexternalhit/1.1 "
               "(+https://www.facebook.com/externalhit_uatext.php)")

# Delivery tier allowlist — https + these host suffixes ONLY.
CDN_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")

# The public web-app id Instagram ships to browsers; used only as a polite
# header for decode slots that want it.  Not a credential — it is public,
# constant, and printed on every instagram.com HTML page.
IG_APP_ID = "936619743392459"

# Shortcode alphabet (URL-safe base64 order).  Shortcodes are a reversible
# big-endian base-64 encoding of the numeric media id — computed locally,
# zero network, a pure truth signal.
SHORTCODE_ALPHABET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "abcdefghijklmnopqrstuvwxyz0123456789-_")

LOG_QUIET = False          # global gate; --quiet flips it
ERRORS: list = []          # shared structured error sink

STAGE_DISCOVERY = "discovery"
STAGE_DECODER = "decoder"
STAGE_FETCHER = "fetcher"
STAGE_ORCHESTRATOR = "orchestrator"

# Stable error codes — the contract consumers branch on.  Never rename.
E_INVALID_INPUT = "E_INVALID_INPUT"
E_SHARE_EXPAND_FAILED = "E_SHARE_EXPAND_FAILED"
E_POST_UNAVAILABLE = "E_POST_UNAVAILABLE"
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


# ── Shortcode ↔ media id (pure math, the local truth signal) ────────────────
def shortcode_to_id(code: str):
    """Decode an Instagram shortcode into its numeric media id.

    Shortcodes are big-endian base-64 over SHORTCODE_ALPHABET.  Returns
    the decimal string, or None if the code contains alien characters.
    """
    n = 0
    for ch in code:
        idx = SHORTCODE_ALPHABET.find(ch)
        if idx < 0:
            return None
        n = n * 64 + idx
    return str(n) if n > 0 else None


def id_to_shortcode(media_id: str):
    """Inverse of shortcode_to_id — used in tests and for canonicalization."""
    try:
        n = int(media_id)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    out = []
    while n:
        n, r = divmod(n, 64)
        out.append(SHORTCODE_ALPHABET[r])
    return "".join(reversed(out))


def _safe_shortcode(value: str):
    """Filename-safe shortcode guard: alphabet + sane length only."""
    if not value or not (5 <= len(value) <= 32):
        return None
    return value if all(c in SHORTCODE_ALPHABET for c in value) else None


def _safe_media_id(value: str):
    """Numeric media-id guard (filename injection defense)."""
    if not value or not re.fullmatch(r"\d{1,25}", value):
        return None
    return value


# ── Discovery: input normalization ──────────────────────────────────────────
_POST_PATHS = ("p", "reel", "reels", "tv")
_IG_HOSTS = ("instagram.com", "www.instagram.com", "m.instagram.com",
             "l.instagram.com", "instagr.am", "www.instagr.am",
             "ddinstagram.com", "www.ddinstagram.com")
_BARE_SC_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
_PATH_SC_RE = re.compile(
    r"^(?:www\.|m\.|l\.)?(?:instagram\.com|instagr\.am|ddinstagram\.com)"
    r"/(?:" + "|".join(_POST_PATHS) + r")/([A-Za-z0-9_-]{5,32})(?:[/?#]|$)",
    re.IGNORECASE)
_SHARE_PATH_RE = re.compile(
    r"^(?:www\.|m\.)?instagram\.com/share(?:/[\w-]+)*"
    r"(/[A-Za-z0-9_-]{2,32})?(?:[/?#]|$)", re.IGNORECASE)


def normalize_input(inp: str) -> dict:
    """Parse one user input into a discovery result.  Raises InputError.

    Accepts:
      * https://www.instagram.com/p/<code>/…        (p | reel | reels | tv)
      * instagr.am / m. / l. / ddinstagram variants
      * share links  (instagram.com/share/…  or  /share/p/…)  — one-hop
      * bare shortcodes  (C…-style, 5–32 alphabet chars)

    Returns {"input", "shortcode", "kind", "share_path", "canonical_url"}.
    `kind` ∈ {"post", "reel", "tv", "share", "bare"}.  canonical_url is the
    best-known web URL; for share inputs it is the share URL itself until
    expanded.
    """
    if not inp or not isinstance(inp, str) or not inp.strip():
        raise InputError(E_INVALID_INPUT, "empty input")
    raw = inp.strip()
    candidate = raw
    if "://" not in candidate:
        if " " in candidate or "\n" in candidate or "\t" in candidate:
            raise InputError(E_INVALID_INPUT,
                             "input contains whitespace; pass one URL or one shortcode")
        if "." in candidate or "/" in candidate:
            candidate = "https://" + candidate.lstrip("/")
        else:
            candidate = candidate  # bare shortcode candidate

    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()

    # Path-based: /p|reel|reels|tv/<code>
    m = _PATH_SC_RE.match(host + parsed.path)
    if m:
        code = m.group(1)
        if not _safe_shortcode(code):
            raise InputError(E_INVALID_INPUT, f"malformed shortcode: {code!r}")
        kind = "reel" if "reel" in parsed.path.lower()[:6] else (
            "tv" if "/tv/" in (host + parsed.path).lower() else "post")
        # /reels/ and /reel/ both mean reel; /p/ and /tv/ keep their kind.
        low_path = (host + parsed.path).lower()
        if "/reel" in low_path:
            kind = "reel"
        elif "/tv/" in low_path:
            kind = "tv"
        else:
            kind = "post"
        canon = f"https://www.instagram.com/{ {'reel': 'reel', 'tv': 'tv'}.get(kind, 'p') }/{code}/"
        return {"input": raw, "shortcode": code, "kind": kind,
                "share_path": None, "canonical_url": canon}

    # Share links (vanity shortlinks) — one-hop expansion, never followed
    # further.  We resolve, we do not crawl.
    if host.endswith("instagram.com") or host.endswith("instagr.am"):
        if "/share" in parsed.path.lower():
            m2 = _SHARE_PATH_RE.match(host + parsed.path)
            share_path = parsed.path
            if m2 and m2.group(1):
                share_path = "/share" + m2.group(1)
            return {"input": raw, "shortcode": None, "kind": "share",
                    "share_path": share_path,
                    "canonical_url": "https://www.instagram.com" + share_path}
        raise InputError(
            E_INVALID_INPUT,
            f"unsupported instagram.com path: {parsed.path or '/'} "
            "(expected /p/, /reel/, /reels/, /tv/ or /share/)")

    # Bare shortcode: alphabet-only token that decodes to a positive id.
    if _BARE_SC_RE.match(raw):
        if shortcode_to_id(raw) is not None:
            return {"input": raw, "shortcode": raw, "kind": "bare",
                    "share_path": None,
                    "canonical_url": f"https://www.instagram.com/p/{raw}/"}
        raise InputError(E_INVALID_INPUT,
                         "bare code does not decode to a media id "
                         f"(check alphabet): {raw!r}")

    raise InputError(E_INVALID_INPUT,
                     f"not a recognized Instagram post URL or shortcode: {raw!r}")


# ── HTTP primitives ─────────────────────────────────────────────────────────
def http_get(url: str, timeout: float = DECODE_TIMEOUT,
             max_bytes: int = EMBED_MAX_BYTES,
             headers: dict = None) -> bytes:
    """Fetch up to max_bytes.  Raises OSError/urllib.error on failure.

    urllib follows redirects by default — one hop for share links, and the
    occasional /p/→/reel/ embed redirect.  Nothing here ever authenticates.
    """
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA_BROWSER})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes + 1)[:max_bytes]


def _headers(ua: str, extra: dict = None) -> dict:
    h = {"User-Agent": ua,
         "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    if extra:
        h.update(extra)
    return h


def _media_url_allowed(url: str) -> bool:
    """Delivery allowlist: https + known Instagram CDN hosts only."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in CDN_SUFFIXES)


def _ext_from_url(url: str, default: str = "jpg") -> str:
    """Best-effort extension from a CDN URL (query params tolerated)."""
    path = urllib.parse.urlsplit(url).path or ""
    m = re.search(r"\.(jpe?g|png|webp|heic|mp4|mov|gif)$", path, re.IGNORECASE)
    return (m.group(1).lower().replace("jpeg", "jpg") if m else default)


def _unquote_html(s: str) -> str:
    """HTML-entity unescape for values pulled out of attributes."""
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'")
             .replace("&#x27;", "'").replace("&#x2F;", "/")
             .replace("&#064;", "@"))


# ── Slot 1: embed_json — the legacy rich embed payload ──────────────────────
def _extract_balanced_json(text: str, start: int) -> str:
    """Return the balanced {...} JSON object starting at `start` ('{')."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, min(len(text), start + EMBED_MAX_BYTES)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _find_shortcode_media(payload: dict, depth: int = 0):
    """Recursively locate the shortcode_media / xdt_shortcode_media object."""
    if depth > 6 or not isinstance(payload, dict):
        return None
    for key in ("shortcode_media", "xdt_shortcode_media"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    for v in payload.values():
        if isinstance(v, dict):
            hit = _find_shortcode_media(v, depth + 1)
            if hit is not None:
                return hit
        elif isinstance(v, list):
            for item in v:
                hit = _find_shortcode_media(item, depth + 1)
                if hit is not None:
                    return hit
    return None


def _decode_json_payload(sm: dict) -> dict:
    """Normalize a rich shortcode_media object into the internal post shape.

    Truth layer: carousel membership comes ONLY from
    edge_sidecar_to_children / carousel_media inside this payload — never
    from page layout.  Anything not in the payload stays null.
    """
    owner = sm.get("owner") or {}
    cap = ""
    edge_cap = sm.get("edge_media_to_caption") or {}
    cap_edges = edge_cap.get("edges") or []
    if cap_edges and isinstance(cap_edges[0], dict):
        cap = (cap_edges[0].get("node") or {}).get("text") or ""
    if not cap and isinstance(sm.get("caption"), str):
        cap = sm["caption"]

    dims = sm.get("dimensions") or {}
    items = []
    typename = sm.get("__typename") or ""
    if typename == "GraphSidecar" or sm.get("edge_sidecar_to_children"):
        children = ((sm.get("edge_sidecar_to_children") or {}).get("edges")) or []
        for i, edge in enumerate(children[:MAX_CAROUSEL_CHILDREN], start=1):
            node = (edge or {}).get("node") or {}
            cdim = node.get("dimensions") or {}
            if node.get("is_video") or node.get("video_url"):
                items.append({"type": "video", "url": node.get("video_url"),
                              "poster_url": node.get("display_url"),
                              "width": cdim.get("width"), "height": cdim.get("height"),
                              "alt": (node.get("accessibility_caption") or None),
                              "duration": node.get("video_duration"),
                              "child_index": i})
            else:
                items.append({"type": "image", "url": node.get("display_url"),
                              "poster_url": None,
                              "width": cdim.get("width"), "height": cdim.get("height"),
                              "alt": (node.get("accessibility_caption") or None),
                              "duration": None,
                              "child_index": i})
    elif sm.get("is_video") or sm.get("video_url"):
        items.append({"type": "video", "url": sm.get("video_url"),
                      "poster_url": sm.get("display_url"),
                      "width": dims.get("width"), "height": dims.get("height"),
                      "alt": (sm.get("accessibility_caption") or None),
                      "duration": sm.get("video_duration"),
                      "child_index": None})
    elif sm.get("display_url"):
        items.append({"type": "image", "url": sm.get("display_url"),
                      "poster_url": None,
                      "width": dims.get("width"), "height": dims.get("height"),
                      "alt": (sm.get("accessibility_caption") or None),
                      "duration": None,
                      "child_index": None})

    media_id = _safe_media_id(str(sm.get("id") or "") or
                              shortcode_to_id(sm.get("shortcode") or "") or "")
    return {
        "media_id": media_id,
        "shortcode": sm.get("shortcode"),
        "kind": "reel" if "Clip" in typename else None,
        "type": "carousel" if len(items) > 1 else
                (items[0]["type"] if items else None),
        "caption": cap or None,
        "created_at": sm.get("taken_at_timestamp"),
        "comment_count": ((sm.get("edge_media_to_comment") or {})
                          .get("count")),
        "like_count": ((sm.get("edge_media_preview_like") or {})
                       .get("count")),
        "author": {
            "username": owner.get("username"),
            "full_name": owner.get("full_name") or None,
            "avatar_url": owner.get("profile_pic_url") or None,
            "verified": bool(owner.get("is_verified")) if owner.get("is_verified") is not None else None,
            "user_id": _safe_media_id(str(owner.get("id"))) if owner.get("id") else None,
            "followers": None,
        },
        "media_items": items,
        "extraction_source": "embed_json",
    }


def _fetch_embed_json(code: str, kind: str):
    """Slot 1: fetch the embed page and mine legacy JSON payloads.

    Outcomes: ("ok", post) | ("unavailable", None) | ("failed", None).
    The rich payload is increasingly rare (Instagram moves it client-side),
    but when present it is the deepest source: caption, metrics, carousel
    children, full author.  When absent we return "failed" and fall through
    to the next slot — the page may still be SSR-rendered for unfurlers.
    """
    path = {"reel": "reel", "tv": "tv"}.get(kind, "p")
    url = f"https://www.instagram.com/{path}/{code}/embed/captioned/"
    try:
        body = http_get(url, timeout=DECODE_TIMEOUT, headers=_headers(UA_BROWSER))
        html = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "unavailable", None
        return "failed", None
    except (OSError, ValueError):
        return "failed", None

    low = html.lower()
    if ("sorry, this page isn" in low or "page not found" in low
            or 'content="noindex' in low and "unavailable" in low):
        return "unavailable", None

    post = None
    # Shape A: window.__additionalDataLoaded('extra', {...});
    m = re.search(r"__additionalDataLoaded\s*\(\s*'[^']*'\s*,\s*", html)
    if m:
        blob = _extract_balanced_json(html, html.index("{", m.end() - 1))
        if blob:
            try:
                post = _find_shortcode_media(json.loads(blob))
            except ValueError:
                post = None
    # Shape B: contextJSON embedded as an escaped JSON string
    if post is None:
        m = re.search(r'"contextJSON"\s*:\s*"', html)
        if m:
            blob = _extract_balanced_json(
                html, html.index("{", m.end() - 1)) if "{" in html[m.end():m.end() + 2] else ""
            if blob:
                for key in ("shortcode_media", "xdt_shortcode_media"):
                    if key in blob:
                        try:
                            post = _find_shortcode_media(json.loads(blob))
                        except ValueError:
                            post = None
                        break
    # Shape C: gql_data / items[0] (info-API-shaped responses)
    if post is None:
        m = re.search(r'"items"\s*:\s*\[\s*{', html)
        if m:
            blob = _extract_balanced_json(html, html.index("{", m.end() - 1))
            if blob:
                try:
                    arr = json.loads("{" + blob + "]}") if False else json.loads(blob)
                    post = arr if isinstance(arr, dict) else None
                except ValueError:
                    post = None

    if post is None:
        return "failed", None
    return "ok", _decode_json_payload(post)


# ── Slot 2: embed_html — the unfurler SSR embed page ────────────────────────
_EMBED_IMG_RE = re.compile(
    r'<img[^>]+class="[^"]*EmbeddedMediaImage[^"]*"[^>]*>', re.IGNORECASE)
_EMBED_VIDEO_RE = re.compile(
    r'<video[^>]+class="[^"]*EmbeddedMediaVideo[^"]*"[^>]*>(.*?)</video>',
    re.IGNORECASE | re.S)
_SRC_RE = re.compile(r'(?:src|data-video-src)="([^"]+)"', re.IGNORECASE)
_VIEWPROFILE_RE = re.compile(
    r'<a[^>]+class="[^"]*ViewProfileButton[^"]*"[^>]+href="'
    r'https://www\.instagram\.com/([A-Za-z0-9._]{1,30})/', re.IGNORECASE)
_FOLLOWERS_RE = re.compile(
    r'class="[^"]*FollowerCountText[^"]*"[^>]*>([^<]+)<', re.IGNORECASE)
_IG_CACHE_KEY_RE = re.compile(r"ig_cache_key=([A-Za-z0-9%=]+)")
_UNAVAILABLE_MARKERS = ("sorry, this page isn", "page not found",
                        "this post is unavailable", "content unavailable")


def _parse_followers(text: str):
    """'4.4M followers' → 4400000 (int) or None.  Honest null on failure."""
    if not text:
        return None
    m = re.match(r"([\d.,]+)\s*([KMB])?", text.strip(), re.IGNORECASE)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(
        (m.group(2) or "").lower(), 1)
    return int(n * mult)


def _fetch_embed_html(code: str, kind: str):
    """Slot 2: SSR embed HTML for link unfurlers.

    When Instagram serves the unauthenticated unfurler view it renders the
    post media straight into the HTML: the image tag (or a video element),
    the author's profile link, and a follower count.  The cache key on the
    media URL base64-encodes the numeric media id — a truth signal we do
    not have to guess.  Captions and carousel membership are NOT exposed
    here; those fields stay honestly null.
    """
    path = {"reel": "reel", "tv": "tv"}.get(kind, "p")
    url = f"https://www.instagram.com/{path}/{code}/embed/captioned/"
    try:
        body = http_get(url, timeout=DECODE_TIMEOUT,
                        headers=_headers(UA_UNFURLER))
        html = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "unavailable", None
        return "failed", None
    except (OSError, ValueError):
        return "failed", None

    low = html.lower()
    if any(marker in low for marker in _UNAVAILABLE_MARKERS):
        return "unavailable", None

    items = []
    # Image item(s)
    for tag in _EMBED_IMG_RE.findall(html)[:MAX_CAROUSEL_CHILDREN]:
        m = _SRC_RE.search(tag)
        if m and "emoji" not in m.group(1):  # skip static emoji sprites
            src = _unquote_html(m.group(1))
            alt = re.search(r'alt="([^"]*)"', tag)
            items.append({"type": "image", "url": src, "poster_url": None,
                          "width": None, "height": None,
                          "alt": _unquote_html(alt.group(1)) if alt else None,
                          "duration": None, "child_index": None})
    # Video item (if the embed serves one)
    for vtag in _EMBED_VIDEO_RE.findall(html)[:MAX_CAROUSEL_CHILDREN]:
        m = _SRC_RE.search(vtag) or _SRC_RE.search(
            html[_EMBED_VIDEO_RE.search(html).start():
                 _EMBED_VIDEO_RE.search(html).start() + 2000])
        if m:
            src = _unquote_html(m.group(1))
            items.append({"type": "video", "url": src, "poster_url": None,
                          "width": None, "height": None, "alt": None,
                          "duration": None, "child_index": None})

    if not items:
        return "failed", None

    media_id = None
    km = _IG_CACHE_KEY_RE.search(html)
    if km:
        try:
            pad = km.group(1).replace("%3D", "=")
            pad += "=" * (-len(pad) % 4)
            decoded = base64.b64decode(pad).decode("ascii", errors="strict")
            media_id = _safe_media_id(decoded)
        except Exception:  # noqa: BLE001 — cache key is best-effort
            media_id = None
    if not media_id:
        media_id = shortcode_to_id(code)

    author = {"username": None, "full_name": None, "avatar_url": None,
              "verified": None, "user_id": None, "followers": None}
    pm = _VIEWPROFILE_RE.search(html)
    if pm:
        author["username"] = pm.group(1)
    fm = _FOLLOWERS_RE.search(html)
    if fm:
        author["followers"] = _parse_followers(fm.group(1))

    return "ok", {
        "media_id": media_id, "shortcode": code,
        "kind": {"reel": "reel", "tv": "tv"}.get(kind, None),
        "type": "carousel" if len(items) > 1 else items[0]["type"],
        "caption": None, "created_at": None, "comment_count": None,
        "like_count": None, "author": author,
        "media_items": items, "extraction_source": "embed_html",
    }


# ── Slot 3: og_meta — the OpenGraph page scrape ─────────────────────────────
def _fetch_og_meta(code: str, kind: str):
    """Slot 3: OpenGraph meta tags on the canonical post page.

    The weakest but most portable door: when Instagram is willing to unfurl
    the post page for a crawler, the og: tags carry title ("username on
    Instagram: "caption""), the primary image, and — for videos —
    og:video:secure_url.  From datacenter IPs this wall usually stands;
    from residential IPs it is a dependable last resort.  Fails closed.
    """
    path = {"reel": "reel", "tv": "tv"}.get(kind, "p")
    url = f"https://www.instagram.com/{path}/{code}/"
    try:
        body = http_get(url, timeout=DECODE_TIMEOUT,
                        headers=_headers(UA_UNFURLER))
        html = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "unavailable", None
        return "failed", None
    except (OSError, ValueError):
        return "failed", None

    low = html.lower()
    if any(marker in low for marker in _UNAVAILABLE_MARKERS):
        return "unavailable", None

    metas = {}
    for m in re.finditer(
            r'<meta[^>]+property="((?:og|twitter):[a-z_:]+)"[^>]+'
            r'content="([^"]*)"', html, re.IGNORECASE):
        metas[m.group(1).lower()] = _unquote_html(m.group(2))
    if not metas:
        return "failed", None

    title = metas.get("og:title", "")
    username = None
    caption = None
    tm = re.match(r"^(.*?)\s+on Instagram:?\.?\s*(.*)$", title or "", re.S)
    if tm:
        username = tm.group(1).strip().lstrip("@") or None
        rest = tm.group(2).strip().strip('"').strip()
        caption = rest or None
        if caption and caption in ("\u2026", "…"):
            caption = None

    items = []
    if metas.get("og:video:secure_url") or metas.get("og:video:url") \
            or metas.get("og:video"):
        items.append({"type": "video",
                      "url": metas.get("og:video:secure_url")
                      or metas.get("og:video:url") or metas.get("og:video"),
                      "poster_url": metas.get("og:image") or None,
                      "width": None, "height": None, "alt": None,
                      "duration": None, "child_index": None})
    elif metas.get("og:image"):
        items.append({"type": "image", "url": metas["og:image"],
                      "poster_url": None, "width": None, "height": None,
                      "alt": None, "duration": None, "child_index": None})

    if not items:
        return "failed", None

    return "ok", {
        "media_id": shortcode_to_id(code), "shortcode": code,
        "kind": {"reel": "reel", "tv": "tv"}.get(kind, None),
        "type": items[0]["type"], "caption": caption,
        "created_at": None, "comment_count": None, "like_count": None,
        "author": {"username": username, "full_name": None,
                   "avatar_url": None, "verified": None, "user_id": None,
                   "followers": None},
        "media_items": items, "extraction_source": "og_meta",
    }


# ── Decode orchestrator: walk the slots, record provenance ──────────────────
# Slot function table (module constant so tests can patch it).  Slots are
# replaceable implementations of one contract — swap a dead one here.
DECODE_SLOT_FNS = [
    ("embed_json", _fetch_embed_json),
    ("embed_html", _fetch_embed_html),
    ("og_meta", _fetch_og_meta),
]


def decode_post(code: str, kind: str):
    """Run every decode slot in order until one returns "ok".

    Returns (post_dict | None, trace) where trace is the provenance list
    [{"slot", "outcome"}] in try order.  A slot failing over to the next
    is designed behavior — it is recorded in the trace, not in the error
    sink.  Only run-level verdicts land in the error sink: an unexpected
    slot crash, or every slot failing (with the honest aggregate code).
    """
    trace = []
    unavailable_seen = False
    for name, fn in DECODE_SLOT_FNS:
        try:
            outcome, post = fn(code, kind)
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
        record_error(STAGE_DECODER, E_POST_UNAVAILABLE,
                     f"all {len(trace)} decode slots report the post "
                     "unavailable", subject=code)
    else:
        record_error(STAGE_DECODER, E_DECODE_FAILED,
                     f"all {len(trace)} decode slots failed to produce "
                     "a payload", subject=code)
    return None, trace


# ── Delivery: verified downloads (never trust a self-report) ────────────────
def _magic_kind(head: bytes):
    """Identify a file by magic bytes — the only verification that counts."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
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
                "User-Agent": UA_BROWSER, "Accept": "*/*"})
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


def _expand_share_link(share_path: str) -> dict:
    """One-hop expansion of a /share/ link.  We read the Location header
    (or the final URL after urllib's single redirect) and re-normalize.
    Never follow more than one hop.  Never crawl."""
    url = "https://www.instagram.com" + share_path
    try:
        # urllib follows the redirect; we read where it landed.
        req = urllib.request.Request(
            url, headers=_headers(UA_UNFURLER), method="HEAD")
        try:
            resp = urllib.request.urlopen(req, timeout=WALK_TIMEOUT)
            landed = resp.geturl()
            resp.close()
        except urllib.error.HTTPError as e:
            landed = e.geturl() if hasattr(e, "geturl") else ""
        if not landed:
            raise OSError("no redirect target")
        return normalize_input(landed)
    except InputError:
        raise
    except Exception as e:  # noqa: BLE001
        raise InputError(
            E_SHARE_EXPAND_FAILED,
            f"could not expand share link (one hop only): "
            f"{type(e).__name__}: {e}")


def _dedupe_media_items(items):
    """Per-URL dedupe preserving first-seen order.  Items without a URL
    are KEPT — they carry the honest 'nothing was exposed' reason."""
    seen = set()
    out = []
    for it in items:
        key = it.get("url") or f"__no_url_{len(out)}__"
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def harvest(norm: dict, out: Path, do_download: bool = True) -> dict:
    """Full pipeline: decode → map → (download) → envelope.  Never raises.

    The envelope is the single source of truth for the caller; everything
    material that happened is in it — statuses, provenance, errors, counts.
    """
    started = time.monotonic()
    code = norm["shortcode"]
    kind = norm["kind"]
    out.mkdir(parents=True, exist_ok=True)

    post, slots_tried = decode_post(code, kind)
    if post is None:
        status = "empty"
        envelope_post = None
        counts = {"images": 0, "videos": 0, "downloaded_media": 0,
                  "failed_downloads": 0}
    else:
        # Truth layer: dedupe by URL; keep payload order only.
        items = _dedupe_media_items(post.get("media_items") or [])
        images = [i for i in items if i["type"] == "image"]
        videos = [i for i in items if i["type"] == "video"]
        children = [i for i in items if i.get("child_index") is not None]

        out.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        failed = 0
        mapped_images = []
        mapped_videos = []
        p_idx = 0
        v_idx = 0
        for item in items:
            url = item.get("url")
            if item["type"] == "video":
                v_idx += 1
                entry = {"url": url, "poster_url": item.get("poster_url"),
                         "width": item.get("width"), "height": item.get("height"),
                         "duration": item.get("duration"),
                         "child_index": item.get("child_index"),
                         "file": None, "poster_file": None,
                         "downloaded": False,
                         "downloadable": bool(url),
                         "reason": None if url else "no_video_url_exposed"}
                if do_download and url and _media_url_allowed(url):
                    fname = f"{code}_v{v_idx}.mp4"
                    okd = download(url, out / fname)
                    entry["downloaded"] = okd
                    entry["file"] = fname if okd else None
                    if not okd:
                        entry["reason"] = entry["reason"] or "download_failed"
                    downloaded += int(okd)
                    failed += int(not okd)
                    if item.get("poster_url") and _media_url_allowed(item["poster_url"]):
                        pfname = f"{code}_v{v_idx}_poster.jpg"
                        okp = download(item["poster_url"], out / pfname)
                        entry["poster_file"] = pfname if okp else None
                        if not okp:
                            record_error(STAGE_FETCHER, E_DOWNLOAD_FAILED,
                                         "poster download failed",
                                         subject=pfname)
                elif url and not _media_url_allowed(url):
                    entry["downloadable"] = False
                    entry["reason"] = "url_outside_delivery_allowlist"
                mapped_videos.append(entry)
            else:
                p_idx += 1
                ext = _ext_from_url(url or "", "jpg")
                entry = {"url": url, "alt": item.get("alt"),
                         "width": item.get("width"), "height": item.get("height"),
                         "child_index": item.get("child_index"),
                         "file": None, "downloaded": False,
                         "downloadable": bool(url), "reason": None}
                if do_download and url and _media_url_allowed(url):
                    fname = f"{code}_p{p_idx}.{ext}"
                    okd = download(url, out / fname)
                    entry["downloaded"] = okd
                    entry["file"] = fname if okd else None
                    if not okd:
                        entry["reason"] = entry["reason"] or "download_failed"
                    downloaded += int(okd)
                    failed += int(not okd)
                elif url and not _media_url_allowed(url):
                    entry["downloadable"] = False
                    entry["reason"] = "url_outside_delivery_allowlist"
                mapped_images.append(entry)

        status = "ok"
        if ERRORS:
            status = "partial"

        envelope_post = {
            "shortcode": post.get("shortcode") or code,
            "media_id": post.get("media_id"),
            "url": norm["canonical_url"],
            "kind": post.get("kind") or (kind if kind != "bare" else None),
            # Truth layer: the type is recomputed from the mapped media
            # items, never trusted from the payload label.
            "type": ("carousel" if len(items) > 1
                     else (items[0]["type"] if items else None)),
            "caption": post.get("caption"),
            "created_at": _iso_from_ts(post.get("created_at")),
            "like_count": post.get("like_count"),
            "comment_count": post.get("comment_count"),
            "author": post.get("author") or {
                "username": None, "full_name": None, "avatar_url": None,
                "verified": None, "user_id": None, "followers": None},
            "extraction_source": post.get("extraction_source"),
            "media": {"images": mapped_images, "videos": mapped_videos},
            "carousel": {
                "known": bool(children),
                "child_count": len(children) if children else
                               (len(items) if len(items) > 1 else None),
            },
        }

    counts = {
        "images": len((envelope_post or {}).get("media", {}).get("images", [])),
        "videos": len((envelope_post or {}).get("media", {}).get("videos", [])),
        "downloaded_media": sum(
            1 for m in (envelope_post or {}).get("media", {}).get("images", [])
            + (envelope_post or {}).get("media", {}).get("videos", [])
            for d in [m] if d.get("downloaded")),
        "failed_downloads": sum(
            1 for m in (envelope_post or {}).get("media", {}).get("images", [])
            + (envelope_post or {}).get("media", {}).get("videos", [])
            if m.get("downloadable") and not m.get("downloaded")
            and m.get("file") is None and m.get("reason") in
            ("download_failed",)),
    }

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "source": {"tool": TOOL_NAME, "version": __version__,
                   "generated_at": datetime.now(timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ")},
        "request": {"input": norm["input"], "shortcode": code,
                    "canonical_url": norm["canonical_url"],
                    "options": {"download_media": bool(do_download)}},
        "status": status,
        "post": envelope_post,
        "errors": list(ERRORS),
        "metadata": {"duration_sec": round(time.monotonic() - started, 3),
                     "decode_slots_tried": slots_tried,
                     "counts": counts},
    }
    _write_manifest(out, envelope)
    return envelope


def _write_manifest(out: Path, envelope: dict) -> bool:
    """Atomic manifest write (`.part` + os.replace, UTF-8, stable key order)."""
    path = out / "post_manifest.json"
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


def main(argv=None) -> int:
    """CLI entry.  Exit 0 = harvested (ok/partial), 1 = nothing/error,
    2 = usage.  `--json` prints exactly one summary object on stdout."""
    ap = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Harvest a public Instagram post/carousel/reel: "
                    "no login, no API keys, no browser, no LLM.")
    ap.add_argument("input", help="Instagram post URL (p/reel/reels/tv), "
                                  "share link, or bare shortcode")
    ap.add_argument("--out", default="ig_media",
                    help="output directory (default: ig_media)")
    ap.add_argument("--no-download", action="store_true",
                    help="metadata only; do not fetch media files")
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

    if norm["kind"] == "share":
        try:
            norm = _expand_share_link(norm["share_path"])
            log(f"[ok ] share link expanded -> {norm['canonical_url']}")
        except InputError as e:
            record_error(STAGE_DISCOVERY, e.code, e.message,
                         subject=norm["share_path"])
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "source": {"tool": TOOL_NAME, "version": __version__,
                           "generated_at": datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")},
                "request": {"input": args.input, "shortcode": None,
                            "canonical_url": norm["canonical_url"],
                            "options": {"download_media": not args.no_download}},
                "status": "empty", "post": None,
                "errors": list(ERRORS),
                "metadata": {"duration_sec": round(time.monotonic() - started, 3),
                             "decode_slots_tried": [], "counts": {}}}
            out = Path(args.out)
            _write_manifest(out, envelope)
            if args.json:
                print(json.dumps(_summary(envelope, out)))
            return 1

    log(f"[..] harvesting {norm['canonical_url']}")
    out = Path(args.out)
    envelope = harvest(norm, out, do_download=not args.no_download)

    summary = _summary(envelope, out)
    ok = envelope["status"] in ("ok", "partial")
    log(f"[{'done' if ok else 'err'}] status={envelope['status']} "
        f"images={summary.get('images', 0)} videos={summary.get('videos', 0)} "
        f"downloaded={summary.get('downloaded', 0)}")
    if args.json:
        print(json.dumps(summary))
    return 0 if ok else 1


def _summary(envelope: dict, out: Path) -> dict:
    """The pipe-safe stdout summary (one JSON object, `--json`)."""
    post = envelope.get("post") or {}
    counts = (envelope.get("metadata") or {}).get("counts") or {}
    return {
        "ok": envelope.get("status") in ("ok", "partial"),
        "status": envelope.get("status"),
        "shortcode": envelope.get("request", {}).get("shortcode"),
        "media_id": post.get("media_id"),
        "canonical_url": envelope.get("request", {}).get("canonical_url"),
        "post_type": post.get("type"),
        "extraction_source": post.get("extraction_source"),
        "images": counts.get("images", 0),
        "videos": counts.get("videos", 0),
        "downloaded": counts.get("downloaded_media", 0),
        "failed_downloads": counts.get("failed_downloads", 0),
        "out_dir": str(out),
        "manifest_path": str(out / "post_manifest.json"),
        "errors": envelope.get("errors", []),
        "duration_sec": (envelope.get("metadata") or {}).get("duration_sec"),
    }


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
