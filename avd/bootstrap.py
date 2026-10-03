"""Bootstrap module — auto-installs external dependencies on first use.

The system has these external deps that auto-install:
  - ffmpeg/ffprobe (system binary — verified via `which ffprobe`)
  - XHS-Downloader repo (Python package — cloned to ~/XHS-Downloader or AVD_XHS_DOWNLOADER_PATH)
  - curl-cffi (pip package, used by Rednote extractor slot 2)
  - Optional: SOCKS5 proxy list (not auto-installed; user-supplied)

This module is idempotent — safe to call on every download.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from avd.utils.logging import get_logger

log = get_logger("avd.bootstrap")

# Default install location for the XHS-Downloader repo
_XHS_DEFAULT_PATHS = [
    Path(os.environ.get("AVD_XHS_DOWNLOADER_PATH", "")),  # explicit override
    Path.home() / "XHS-Downloader",
    Path("/home/z/my-project/XHS-Downloader"),
    Path("/opt/XHS-Downloader"),
    Path("/tmp/XHS-Downloader"),
]

XHS_REPO_URL = "https://github.com/JoeanAmier/XHS-Downloader.git"

# Required XHS-Downloader Python deps (beyond what avd already installs)
XHS_EXTRA_DEPS = [
    "curl-cffi>=0.7",
    "fastapi>=0.110",
    "fastmcp>=4.0",
    "textual>=8.0",
    "pyperclip>=1.8",
    "emoji>=2.10",
    "aiosqlite>=0.20",
    "lxml>=5.2",
]


def find_xhs_downloader() -> str | None:
    """Return the path to a cloned XHS-Downloader repo, or None."""
    # First check env var override
    env_path = os.environ.get("AVD_XHS_DOWNLOADER_PATH")
    if env_path and Path(env_path).exists() and (Path(env_path) / "source" / "application" / "app.py").exists():
        return env_path
    # Then check default locations
    for p in _XHS_DEFAULT_PATHS:
        if not p:
            continue
        if (p / "source" / "application" / "app.py").exists():
            return str(p)
    return None


def ensure_xhs_downloader() -> str | None:
    """Ensure XHS-Downloader is cloned and deps installed. Returns path or None on failure."""
    existing = find_xhs_downloader()
    if existing:
        # Already there — just make sure deps are installed (cheap check)
        _ensure_xhs_deps(existing)
        return existing

    # Clone to ~/XHS-Downloader (or AVD_XHS_DOWNLOADER_PATH if set)
    target_str = os.environ.get("AVD_XHS_DOWNLOADER_PATH") or str(Path.home() / "XHS-Downloader")
    target = Path(target_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("bootstrap_cloning_xhs", target=str(target))
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", XHS_REPO_URL, str(target)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as e:
        log.error("bootstrap_clone_failed", error=e.stderr.decode(errors="replace")[:200] if e.stderr else str(e))
        return None
    except subprocess.TimeoutExpired:
        log.error("bootstrap_clone_timeout")
        return None

    _ensure_xhs_deps(str(target))
    return str(target)


def _ensure_xhs_deps(repo_path: str) -> bool:
    """Install XHS-Downloader's Python deps via pip."""
    # Try to import fastmcp (a heavy dep that's only needed for XHS-Downloader)
    try:
        import fastmcp  # noqa: F401
        import fastapi  # noqa: F401
        import textual  # noqa: F401
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        log.info("bootstrap_installing_xhs_deps")
    # Install via pip
    cmd = [sys.executable, "-m", "pip", "install", "--break-system-packages", "--quiet"] + XHS_EXTRA_DEPS
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        log.info("bootstrap_xhs_deps_installed")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("bootstrap_xhs_deps_failed", error=str(e)[:200])
        return False


def ensure_ffmpeg() -> bool:
    """Check ffmpeg is on PATH. Returns True if available."""
    return shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None


def ensure_pip_deps() -> bool:
    """Check that all avd runtime deps are importable."""
    critical = ["httpx", "pydantic", "tenacity", "rich", "click", "yt_dlp", "aiofiles", "aiosqlite", "feedparser", "bs4", "lxml", "magic", "structlog", "Levenshtein"]
    for mod in critical:
        try:
            __import__(mod)
        except ImportError:
            log.warning("bootstrap_missing_dep", module=mod)
            return False
    return True


def full_bootstrap() -> dict:
    """Run all bootstrap checks. Returns dict with status of each step."""
    result = {
        "ffmpeg": ensure_ffmpeg(),
        "pip_deps": ensure_pip_deps(),
        "xhs_downloader_path": find_xhs_downloader(),
    }
    if not result["xhs_downloader_path"]:
        result["xhs_downloader_path"] = ensure_xhs_downloader()
    result["ready"] = bool(result["ffmpeg"] and result["pip_deps"] and result["xhs_downloader_path"])
    return result
