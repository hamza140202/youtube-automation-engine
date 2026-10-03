"""Tester agent — runs end-to-end on curated sample URLs."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from avd.models import TestReport
from avd.orchestrator import Orchestrator
from avd.utils.logging import get_logger

log = get_logger("avd.tester")
console = Console(stderr=True)

SAMPLE_URLS_PATH = Path(__file__).parent.parent.parent / "tests" / "sample_urls.json"
RESULTS_DIR = Path(__file__).parent.parent.parent / "tests" / "results"


def _load_samples() -> dict[str, list[str]]:
    """Load sample URLs. Falls back to embedded defaults if file missing."""
    if SAMPLE_URLS_PATH.exists():
        with open(SAMPLE_URLS_PATH) as f:
            return json.load(f)
    return {
        "tiktok": [
            "https://www.tiktok.com/@scout2015/video/6718335390845095173",
        ],
        "twitter": [
            "https://x.com/jack/status/20",
        ],
        "reddit": [
            "https://www.reddit.com/r/aww/comments/1bxxhzp/my_cat_is_beautiful/",
        ],
        "instagram": [
            "https://www.instagram.com/p/CxYz1234567/",
        ],
        "rednote": [
            "https://www.xiaohongshu.com/explore/64dxxexampleid000000000001",
        ],
        "douyin": [
            "https://www.douyin.com/video/7126745726494821640",
        ],
    }


class Tester:
    """Self-test the entire system end-to-end."""

    __version__ = "1.0.0"

    def __init__(self) -> None:
        self.orchestrator = Orchestrator()

    def samples(self) -> dict[str, list[str]]:
        return _load_samples()

    async def run(self, platforms: list[str] | None = None, *, smoke: bool = True) -> list[TestReport]:
        samples = self.samples()
        if platforms:
            samples = {k: v for k, v in samples.items() if k in platforms}

        tasks: list[tuple[str, str]] = []
        for plat, urls in samples.items():
            if smoke:
                tasks.append((plat, urls[0]))
            else:
                for u in urls:
                    tasks.append((plat, u))

        # Run serially to be polite (one request at a time per platform)
        reports: list[TestReport] = []
        for platform, url in tasks:
            t0 = time.time()
            try:
                result = await self.orchestrator.download(
                    url, dest=Path(f"./download/{platform}"), opts={}
                )
                duration = time.time() - t0
                reports.append(TestReport(
                    platform=platform,
                    sample_url=url,
                    extractor_used=result.extractor_chain[-1] if result.extractor_chain else None,
                    downloaded=result.status == "ok",
                    verified=result.verifier_report.integrity_ok if result.verifier_report else False,
                    truth_checked=result.truth_report is not None,
                    truth_verdict=result.truth_report.verdict if result.truth_report else "skipped",
                    duration_s=round(duration, 2),
                    artifact_path=result.artifact_path,
                    error=result.reason or result.error,
                ))
            except Exception as e:
                duration = time.time() - t0
                reports.append(TestReport(
                    platform=platform,
                    sample_url=url,
                    duration_s=round(duration, 2),
                    error=f"{type(e).__name__}: {e}",
                ))
            # Be polite between platforms
            await asyncio.sleep(0.5)

        # Print rich table
        self._print_table(reports)
        # Write JSON results
        self._write_results(reports)
        return reports

    def _print_table(self, reports: list[TestReport]) -> None:
        table = Table(title="avd test results", show_lines=True)
        table.add_column("Platform", style="cyan")
        table.add_column("URL", style="white", overflow="fold")
        table.add_column("Extractor", style="magenta")
        table.add_column("Downloaded", style="green")
        table.add_column("Verified", style="green")
        table.add_column("Truth", style="yellow")
        table.add_column("Time(s)", justify="right")
        table.add_column("Error", style="red", overflow="fold")

        for r in reports:
            table.add_row(
                r.platform,
                r.sample_url[:80],
                r.extractor_used or "-",
                "✅" if r.downloaded else "❌",
                "✅" if r.verified else "❌",
                r.truth_verdict,
                f"{r.duration_s:.2f}",
                r.error or "-",
            )
        console.print(table)

    def _write_results(self, reports: list[TestReport]) -> None:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = RESULTS_DIR / f"{ts}.json"
        with open(path, "w") as f:
            json.dump([r.model_dump(mode="json") for r in reports], f, indent=2, default=str)
        console.print(f"\n[dim]Results written to {path}[/]")
