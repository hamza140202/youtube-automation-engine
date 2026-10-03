"""Reddit extractor — fallback chain verified live 2026-10-03.

WORKING METHODS (verified live from datacenter IP):

Chain (priority order):
  1. rapidsave.com/info + v.redd.it CMAF direct + ffmpeg mux  (PRIMARY — verified)
  2. rapidsave.com/info + sd.rapidsave.com/download.php server-side mux  (simpler fallback)
  3. yt-dlp Reddit + OAuth2 (if AVD_REDDIT_* env vars set — last resort)
  4. RSS feed + preview.redd.it image extraction (image-only last resort)

KEY DISCOVERY: v.redd.it CMAF URLs ARE NOT 403'd from datacenter IPs!
The previous research report (Task 2-a) was wrong on this. We can fetch
v.redd.it/<id>/CMAF_<1080|720|480|360>.mp4 and CMAF_AUDIO_128.mp4 directly.
We just need rapidsave to discover the v.redd.it ID (since /comments/<id>.json
IS 403'd). Then we mux video+audio locally with ffmpeg.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

from avd.config import get_settings
from avd.extractors.base import BaseExtractor
from avd.extractors.registry import ExtractorRegistry
from avd.models import DownloadMetadata, ExtractOutcome, ExtractorMeta
from avd.utils.fs import detect_file_type, ensure_dir, safe_unlink
from avd.utils.http import fetch_text, stream_download
from avd.utils.logging import get_logger

log = get_logger("avd.extractors.reddit")

REDDIT_URL_PATTERNS = [
    r"^https?://(?:www\.|old\.|new\.|np\.)?reddit\.com/.+",
    r"^https?://redd\.it/.+",
    r"^https?://v\.redd\.it/.+",
]

# Per research: v.redd.it CMAF CDN IS open from datacenter IPs
REDDIT_CDN_ALLOWLIST = (
    "v.redd.it",
    "redditmedia.com",
    "reddit.com",
    "redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    "i.redd.it",
    "rapidsave.com",
    "sd.rapidsave.com",
)


def _is_allowed_cdn(url: str) -> bool:
    return any(host in url.lower() for host in REDDIT_CDN_ALLOWLIST)


def _extract_post_id(url: str) -> str | None:
    m = re.search(r"/comments/([a-z0-9]{5,})", url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/([a-z0-9]{5,})(?:/|$|\?)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


async def _fetch_rapidsave_info(post_url: str) -> dict | None:
    """Fetch rapidsave.com/info?url=... and parse the form-encoded result.

    Returns: {"video_url": "...", "audio_url": "...", "permalink": "..."}
    """
    api = f"https://rapidsave.com/info?url={post_url}"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    from avd.utils.http import get_client
    client = await get_client()
    try:
        r = await client.get(api, headers=headers, timeout=25.0)
        if r.status_code != 200:
            return None
        text = r.text
    except Exception as e:
        log.warning("rapidsave_info_failed", error=str(e))
        return None

    # rapidsave returns form-encoded text like:
    # permalink=...&video_url=https://v.redd.it/...&audio_url=https://v.redd.it/...&time=...
    video_match = re.search(r'video_url=(https://v\.redd\.it/[^&" <]+)', text)
    audio_match = re.search(r'audio_url=(https://v\.redd\.it/[^&" <]+)', text)

    if not video_match and not audio_match:
        # Try the raw v.redd.it IDs directly
        v_ids = re.findall(r'https://v\.redd\.it/([a-z0-9]+)/', text)
        if v_ids:
            vid_id = v_ids[0]
            return {
                "video_url": f"https://v.redd.it/{vid_id}/CMAF_1080.mp4",
                "audio_url": f"https://v.redd.it/{vid_id}/CMAF_AUDIO_128.mp4",
                "permalink": post_url,
                "v_redd_it_id": vid_id,
            }
        return None

    return {
        "video_url": video_match.group(1) if video_match else None,
        "audio_url": audio_match.group(1) if audio_match else None,
        "permalink": post_url,
    }


async def _try_rapidsave_direct_mux(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 1: rapidsave.com/info → v.redd.it CMAF direct → ffmpeg mux.

    Verified working from datacenter IP 2026-10-03.
    No third party sees video bytes (only metadata).
    """
    pid = _extract_post_id(url)
    if not pid:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error="no_post_id")

    info = await _fetch_rapidsave_info(url)
    if not info:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error="rapidsave_info_failed")

    # Extract v.redd.it ID from the URLs
    v_url = info.get("video_url") or ""
    a_url = info.get("audio_url") or ""
    vid_id_match = re.search(r'v\.redd\.it/([a-z0-9]+)', v_url + a_url)
    if not vid_id_match:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error="no_v_redd_it_id")
    vid_id = vid_id_match.group(1)

    # Resolution ladder — probe and pick first that returns 200
    # Try BOTH CMAF_<RES>.mp4 (newer, separate audio) and DASH_<RES>.mp4 (older, audio embedded)
    chosen_res = None
    chosen_res_url = None
    if v_url and ("CMAF_" in v_url or "DASH_" in v_url):
        # rapidsave gave us a specific resolution URL — use it
        chosen_res_url = v_url
    else:
        # Probe the ladder — try CMAF first, then DASH
        from avd.utils.http import get_client
        client = await get_client()
        for fmt in ["CMAF", "DASH"]:
            for res in ["1080", "720", "480", "360"]:
                probe_url = f"https://v.redd.it/{vid_id}/{fmt}_{res}.mp4"
                try:
                    r = await client.head(probe_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5.0)
                    if r.status_code == 200:
                        chosen_res = res
                        chosen_res_url = probe_url
                        break
                except Exception:
                    continue
            if chosen_res:
                break
        if not chosen_res_url:
            return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error="no_resolution_probed")
        audio_url = f"https://v.redd.it/{vid_id}/CMAF_AUDIO_128.mp4"
    if not chosen_res and "CMAF_" not in (v_url or "") and "DASH_" not in (v_url or ""):
        # Fallback to whatever rapidsave gave us
        chosen_res_url = v_url

    if not _is_allowed_cdn(chosen_res_url):
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error="cdn_not_allowed")

    out_path = dest if str(dest).endswith(".mp4") else dest / f"{pid}.mp4"
    ensure_dir(out_path.parent)

    # Download video stream
    v_part = str(out_path) + ".v.mp4"
    ok_v, _, err_v = await stream_download(chosen_res_url, v_part, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.reddit.com/"})
    if not ok_v:
        safe_unlink(v_part)
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error=f"video_download_{err_v}")

    # Download audio stream
    a_part = str(out_path) + ".a.mp4"
    audio_url_final = a_url if a_url else f"https://v.redd.it/{vid_id}/CMAF_AUDIO_128.mp4"
    ok_a, _, err_a = await stream_download(audio_url_final, a_part, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.reddit.com/"})

    if not ok_a:
        # Video-only fallback — many reddit videos have audio embedded in CMAF_<RES>.mp4 if probe-200 says so
        # Try moving the video file as-is
        import os
        try:
            os.replace(v_part, str(out_path))
        except OSError:
            safe_unlink(v_part)
            return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error=f"audio_download_{err_a}")
    else:
        # Mux video + audio with ffmpeg
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
               "-i", v_part, "-i", a_part, "-c", "copy", "-shortest", str(out_path)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                # Fallback to video-only
                import os
                try:
                    os.replace(v_part, str(out_path))
                except OSError:
                    pass
            safe_unlink(v_part)
            safe_unlink(a_part)
        except Exception as e:
            safe_unlink(v_part)
            safe_unlink(a_part)
            return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error=f"ffmpeg_mux_{e}")

    # Verify the final file
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_direct", error=f"magic_byte_{kind}")

    metadata = DownloadMetadata(
        source_platform_post_id=pid,
        title=f"reddit:{pid}",
    )
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=metadata,
        extractor_name="reddit:rapidsave_direct",
        raw_info=info,
    )


async def _try_rapidsave_server_mux(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 2: rapidsave.com server-side mux via sd.rapidsave.com/download.php."""
    pid = _extract_post_id(url)
    if not pid:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_server", error="no_post_id")

    info = await _fetch_rapidsave_info(url)
    if not info:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_server", error="rapidsave_info_failed")

    v_url = info.get("video_url")
    a_url = info.get("audio_url")
    if not v_url:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_server", error="no_video_url_in_info")

    download_api = "https://sd.rapidsave.com/download.php"
    full = f"{download_api}?permalink={url}&video_url={v_url}&audio_url={a_url or ''}"
    out_path = dest if str(dest).endswith(".mp4") else dest / f"{pid}.mp4"
    ensure_dir(out_path.parent)

    ok, _, err = await stream_download(full, str(out_path), headers={"User-Agent": "Mozilla/5.0"})
    if not ok:
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_server", error=f"download_{err}")
    kind, _ = detect_file_type(out_path)
    if kind not in ("mp4", None):
        safe_unlink(out_path)
        return ExtractOutcome(ok=False, extractor_name="reddit:rapidsave_server", error=f"magic_byte_{kind}")
    return ExtractOutcome(
        ok=True,
        artifact_path=Path(out_path),
        metadata=DownloadMetadata(source_platform_post_id=pid),
        extractor_name="reddit:rapidsave_server",
        raw_info=info,
    )


async def _try_ytdlp_oauth(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 3: yt-dlp Reddit with OAuth2 token (last resort — needs AVD_REDDIT_* env vars)."""
    s = get_settings()
    if not (s.reddit_client_id and s.reddit_client_secret and s.reddit_username and s.reddit_password):
        return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error="oauth_not_configured")

    pid = _extract_post_id(url)
    if not pid:
        return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error="no_post_id")

    try:
        import yt_dlp
    except ImportError:
        return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error="yt_dlp_not_installed")

    # Fetch OAuth token
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(s.reddit_client_id, s.reddit_client_secret),
                data={
                    "grant_type": "password",
                    "username": s.reddit_username,
                    "password": s.reddit_password,
                },
                headers={"User-Agent": s.reddit_user_agent},
            )
            if r.status_code != 200:
                return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error="oauth_token_failed")
            token = r.json().get("access_token")
            if not token:
                return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error="no_token_in_response")
    except Exception as e:
        return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error=f"oauth_exception_{e}")

    out_path = dest if str(dest).endswith(".mp4") else dest / f"{pid}.mp4"
    ensure_dir(out_path.parent)
    ytdlp_opts = {
        "outtmpl": str(out_path),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "Authorization": f"Bearer {token}",
            "User-Agent": s.reddit_user_agent,
        },
    }
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error="ytdlp_extract_failed")
            return ExtractOutcome(
                ok=True,
                artifact_path=Path(out_path),
                metadata=DownloadMetadata(
                    source_platform_post_id=pid,
                    title=info.get("title"),
                    author=info.get("uploader"),
                    duration_s=info.get("duration"),
                ),
                extractor_name="reddit:ytdlp",
            )
    except Exception as e:
        return ExtractOutcome(ok=False, extractor_name="reddit:ytdlp", error=str(e))


async def _try_rss_image(url: str, dest: Path, opts: dict) -> ExtractOutcome:
    """Slot 4: RSS feed — metadata + preview.redd.it image (image-only fallback)."""
    pid = _extract_post_id(url)
    if not pid:
        return ExtractOutcome(ok=False, extractor_name="reddit:rss", error="no_post_id")
    rss_url = url.split("?")[0].rstrip("/") + "/.rss"
    status, text = await fetch_text(rss_url, headers={"User-Agent": "python:avd:1.0.0"})
    if status != 200 or not text:
        return ExtractOutcome(ok=False, extractor_name="reddit:rss", error=f"http_{status}")
    img_match = re.search(r"https://preview\.redd\.it/[^\s\"<]+\.jpg", text)
    if img_match:
        img_url = img_match.group(0).replace("&amp;", "&")
        if not _is_allowed_cdn(img_url):
            return ExtractOutcome(ok=False, extractor_name="reddit:rss", error="cdn_not_allowed")
        out_path = dest if str(dest).endswith((".jpg", ".png")) else dest / f"{pid}.jpg"
        ensure_dir(out_path.parent)
        ok, _, err = await stream_download(img_url, str(out_path))
        if not ok:
            return ExtractOutcome(ok=False, extractor_name="reddit:rss", error=f"download_{err}")
        return ExtractOutcome(
            ok=True,
            artifact_path=Path(out_path),
            metadata=DownloadMetadata(source_platform_post_id=pid, title="RSS metadata-only"),
            extractor_name="reddit:rss",
        )
    return ExtractOutcome(ok=False, extractor_name="reddit:rss", error="no_media_in_rss")


class RedditExtractor(BaseExtractor):
    meta = ExtractorMeta(
        name="reddit",
        priority=130,
        url_patterns=REDDIT_URL_PATTERNS,
        platforms=["reddit"],
        description="Reddit fallback chain: rapidsave.com/info → v.redd.it CMAF direct + ffmpeg mux → rapidsave server-side mux → yt-dlp+OAuth → RSS image",
    )

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        opts = opts or {}
        if "redd.it/" in url and "v.redd.it" not in url:
            from avd.utils.http import head_url
            expanded = await head_url(url)
            if expanded:
                url = expanded

        for slot in (_try_rapidsave_direct_mux, _try_rapidsave_server_mux, _try_ytdlp_oauth, _try_rss_image):
            try:
                outcome = await slot(url, dest, opts)
                if outcome.ok:
                    log.info("reddit_slot_ok", slot=outcome.extractor_name, url=url)
                    return outcome
                log.info("reddit_slot_fail", slot=outcome.extractor_name, error=outcome.error, url=url)
            except Exception as e:
                log.warning("reddit_slot_exception", slot=slot.__name__, error=str(e))

        return ExtractOutcome(ok=False, extractor_name="reddit:chain", error="all_slots_failed")


ExtractorRegistry.register(RedditExtractor())
