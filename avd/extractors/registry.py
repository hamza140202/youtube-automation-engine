"""Extractor registry — auto-discovery + per-URL candidate list."""
from __future__ import annotations

import importlib
import re
from typing import ClassVar

from avd.extractors.base import Extractor
from avd.models import ExtractorMeta


class ExtractorRegistry:
    """Holds all known extractors. Picks candidates per URL by pattern match."""

    _extractors: ClassVar[list[Extractor]] = []

    @classmethod
    def register(cls, extractor: Extractor) -> None:
        """Register an extractor instance."""
        cls._extractors.append(extractor)

    @classmethod
    def reset(cls) -> None:
        """Clear all registrations (test helper)."""
        cls._extractors.clear()
        setattr(cls, "_loaded", False)

    @classmethod
    def all(cls) -> list[Extractor]:
        """Return all registered extractors."""
        return list(cls._extractors)

    @classmethod
    def candidates(cls, url: str) -> list[tuple[Extractor, int]]:
        """Return extractors whose url_patterns match, sorted by priority.

        Returns: list of (extractor, priority) — priority ascending.
        """
        out: list[tuple[Extractor, int]] = []
        for ex in cls._extractors:
            for pat in ex.meta.url_patterns:
                try:
                    if re.search(pat, url, re.IGNORECASE):
                        out.append((ex, ex.meta.priority))
                        break
                except re.error:
                    continue
        out.sort(key=lambda t: t[1])
        return out

    @classmethod
    def supported_platforms(cls) -> list[str]:
        """Return the set of platforms covered."""
        plat: set[str] = set()
        for ex in cls._extractors:
            plat.update(ex.meta.platforms)
        return sorted(plat)


def _autoload() -> None:
    """Auto-import all extractor modules so their @register-decorated classes run.

    Called once at package import. Idempotent — uses a sentinel flag.
    """
    if getattr(ExtractorRegistry, "_loaded", False):
        return
    modules = [
        "avd.extractors.tiktok",
        "avd.extractors.twitter",
        "avd.extractors.reddit",
        "avd.extractors.instagram",
        "avd.extractors.rednote",
        "avd.extractors.douyin",
    ]
    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
        except Exception as e:  # pragma: no cover — diagnostic
            import structlog

            structlog.get_logger().warning(
                "extractor_module_load_failed",
                module=mod_name,
                error=str(e),
            )
    ExtractorRegistry._loaded = True  # type: ignore[attr-defined]


_autoload()
