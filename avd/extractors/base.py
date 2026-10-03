"""Extractor Protocol + base helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from avd.models import ExtractOutcome, ExtractorMeta


@runtime_checkable
class Extractor(Protocol):
    """Plugin contract for every platform-specific decoder.

    Implementations live in `avd.extractors.<platform>` and register
    themselves via `ExtractorRegistry.register()` (or auto-discovery).
    """

    meta: ExtractorMeta

    async def extract(self, url: str, *, dest: Path, opts: dict | None = None) -> ExtractOutcome:
        """Try to download `url` to `dest`. Returns ExtractOutcome.

        - `ok=True` only if a verified-eligible artifact was written to disk.
        - `ok=False` with `error` set is the honest-negative path.
        - Never raise — always return ExtractOutcome.
        """
        ...


class BaseExtractor:
    """Convenience base class implementing the Extractor protocol.

    Subclasses must:
      - Set `meta` (ExtractorMeta instance)
      - Implement `async def extract(self, url, *, dest, opts) -> ExtractOutcome`
    """

    meta: ExtractorMeta

    def __init__(self) -> None:
        if not hasattr(self, "meta"):
            raise NotImplementedError(f"{type(self).__name__} must set `meta`")
