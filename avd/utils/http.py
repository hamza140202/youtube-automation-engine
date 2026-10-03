"""Shared async HTTP client (httpx)."""
from __future__ import annotations

from typing import Any

import httpx

from avd.config import get_settings

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    """Singleton async HTTP client. Use this everywhere — never instantiate httpx directly.

    Honors `AVD_PROXY` and `AVD_USER_AGENT` settings.
    """
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    headers = {
        "User-Agent": settings.user_agent,
        "Accept-Language": "en-US,en;q=0.9",
    }
    transport_kwargs: dict[str, Any] = {}
    if settings.proxy:
        transport_kwargs["proxy"] = settings.proxy
    _client = httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(settings.http_timeout_s),
        follow_redirects=True,
        **transport_kwargs,
    )
    return _client


async def close_client() -> None:
    """Close the singleton client (call at process exit)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def fetch_text(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    """GET URL, return (status_code, text). Never raises."""
    client = await get_client()
    try:
        r = await client.get(url, headers=headers or {})
        return r.status_code, r.text
    except Exception as e:
        return -1, f"<fetch error: {e}>"


async def fetch_json(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict | list | None, str]:
    """GET URL, return (status_code, parsed_json_or_None, raw_text)."""
    client = await get_client()
    try:
        r = await client.get(url, headers=headers or {})
        text = r.text
        try:
            return r.status_code, r.json(), text
        except Exception:
            return r.status_code, None, text
    except Exception as e:
        return -1, None, f"<fetch error: {e}>"


async def head_url(url: str, *, headers: dict[str, str] | None = None) -> str | None:
    """HEAD-redirect URL, return final URL after redirects (None on failure).

    Useful for short-link expansion (`vm.tiktok.com/...`, `xhslink.com/...`).
    """
    client = await get_client()
    try:
        # use GET with stream=False to honor redirects without downloading body
        r = await client.get(url, headers=headers or {}, follow_redirects=True)
        return str(r.url)
    except Exception:
        return None


async def stream_download(url: str, dest: str, *, headers: dict[str, str] | None = None) -> tuple[bool, int, str | None]:
    """Stream `url` to `dest` atomically. Returns (ok, size_bytes, error).

    Implementation: stream to `<dest>.part`, verify size > 0, then `os.replace`.
    """
    import asyncio
    import os

    client = await get_client()
    part = f"{dest}.part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    try:
        async with client.stream("GET", url, headers=headers or {}) as r:
            if r.status_code >= 400:
                return False, 0, f"HTTP {r.status_code}"
            total = 0
            with open(part, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
            if total == 0:
                return False, 0, "empty_response"
            os.replace(part, dest)
            return True, total, None
    except Exception as e:
        # Clean up .part on failure
        try:
            os.remove(part)
        except OSError:
            pass
        return False, 0, str(e)
