"""Orchestrator agent — drives the full download pipeline."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from avd.config import get_settings
from avd.extractors.registry import ExtractorRegistry
from avd.models import (
    DownloadMetadata,
    DownloadResult,
    ExtractOutcome,
    SlotAttempt,
)
from avd.utils.breaker import host_of, is_open, record_failure, record_success
from avd.utils.logging import get_logger
from avd.utils.state import save_job, write_dlq
from avd.verifier import Verifier

log = get_logger("avd.orchestrator")

# Global per-host semaphore map
_host_semaphores: dict[str, asyncio.Semaphore] = {}
_global_sem: asyncio.Semaphore | None = None


def _get_host_sem(host: str) -> asyncio.Semaphore:
    if host not in _host_semaphores:
        s = get_settings()
        _host_semaphores[host] = asyncio.Semaphore(s.max_concurrency_per_host)
    return _host_semaphores[host]


def _get_global_sem() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        s = get_settings()
        _global_sem = asyncio.Semaphore(s.max_concurrency_global)
    return _global_sem


def _detect_platform(url: str) -> str:
    """Cheap platform detection from URL."""
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


class Orchestrator:
    """Top-level driver. Callers only ever talk to this agent."""

    __version__ = "1.0.0"

    def __init__(self) -> None:
        self.registry = ExtractorRegistry
        self.verifier = Verifier()
        # TruthAgent lazily imported to avoid circular import on registry load
        from avd.truth_agent import TruthAgent

        self.truth = TruthAgent()

    async def download(self, url: str, *, dest: Path, opts: dict | None = None) -> DownloadResult:
        """Single URL → single artifact. Drives the full chain."""
        opts = opts or {}
        job_id = opts.get("job_id") or str(uuid.uuid4())
        started = datetime.utcnow()
        platform = _detect_platform(url)
        slots_tried: list[SlotAttempt] = []
        extractor_chain: list[str] = []

        # Resolve dest
        dest = Path(dest)
        # If dest is a dir, leave it — extractor will pick filename
        # If dest is a file path, pass through

        # Get candidates
        async with _get_global_sem():
            candidates = self.registry.candidates(url)

            if not candidates:
                result = DownloadResult(
                    url=url,
                    platform=platform,
                    status="empty",
                    reason="no_extractor_match",
                    job_id=job_id,
                    started_at=started,
                    finished_at=datetime.utcnow(),
                )
                save_job(result.model_dump(mode="json"))
                return result

            for extractor, _priority in candidates:
                host = host_of(url)
                # Per-host semaphore
                async with _get_host_sem(host):
                    slot_start = time.time()
                    slot_name = extractor.meta.name
                    extractor_chain.append(slot_name)

                    # Circuit breaker check
                    if is_open(host):
                        slots_tried.append(SlotAttempt(
                            extractor_name=slot_name,
                            outcome="circuit_open",
                            failure_signal=f"breaker_open:{host}",
                            duration_s=time.time() - slot_start,
                        ))
                        continue

                    try:
                        outcome: ExtractOutcome = await extractor.extract(url, dest=dest, opts=opts)
                    except Exception as e:
                        log.warning("orchestrator_extractor_exception", extractor=slot_name, error=str(e))
                        record_failure(host)
                        slots_tried.append(SlotAttempt(
                            extractor_name=slot_name,
                            outcome="extractor_failed",
                            failure_signal=f"exception:{type(e).__name__}",
                            duration_s=time.time() - slot_start,
                        ))
                        continue

                    if not outcome.ok:
                        record_failure(host)
                        slots_tried.append(SlotAttempt(
                            extractor_name=slot_name,
                            outcome="extractor_failed",
                            failure_signal=outcome.error,
                            duration_s=time.time() - slot_start,
                        ))
                        continue

                    # Verify
                    if outcome.artifact_path is None:
                        slots_tried.append(SlotAttempt(
                            extractor_name=slot_name,
                            outcome="verifier_rejected",
                            failure_signal="no_artifact_path",
                            duration_s=time.time() - slot_start,
                        ))
                        continue

                    expected_meta = outcome.metadata.model_dump() if outcome.metadata else None
                    verifier_report = await self.verifier.verify(
                        outcome.artifact_path,
                        expected_meta=expected_meta,
                    )

                    if not verifier_report.integrity_ok:
                        slots_tried.append(SlotAttempt(
                            extractor_name=slot_name,
                            outcome="verifier_rejected",
                            failure_signal=";".join(verifier_report.issues),
                            duration_s=time.time() - slot_start,
                        ))
                        continue

                    record_success(host)

                    # Truth cross-check
                    truth_report = None
                    if not opts.get("no_truth"):
                        try:
                            truth_report = await self.truth.cross_check(
                                url,
                                outcome.metadata.model_dump() if outcome.metadata else {},
                            )
                        except Exception as e:
                            log.warning("truth_failed", error=str(e))
                            truth_report = None

                    finished = datetime.utcnow()
                    result = DownloadResult(
                        url=url,
                        platform=platform,
                        status="ok",
                        artifact_path=outcome.artifact_path,
                        metadata=outcome.metadata,
                        extractor_chain=extractor_chain,
                        slots_tried=slots_tried,
                        verifier_report=verifier_report,
                        truth_report=truth_report,
                        job_id=job_id,
                        started_at=started,
                        finished_at=finished,
                    )
                    save_job(result.model_dump(mode="json"))
                    return result

        # All extractors exhausted — DLQ + honest empty
        finished = datetime.utcnow()
        # Determine the most likely honest-empty reason
        reasons = [s.failure_signal for s in slots_tried if s.failure_signal]
        reason = "all_extractors_exhausted"
        # Promote honest empties
        for r in reasons:
            if r in ("oauth_required", "datacenter_ip_walled", "xsec_token_missing"):
                reason = r
                break

        dlq_id = f"dlq-{int(finished.timestamp())}-{uuid.uuid4().hex[:8]}"
        write_dlq({
            "id": dlq_id,
            "job_id": job_id,
            "url": url,
            "platform": platform,
            "slots_tried": [s.model_dump(mode="json") for s in slots_tried],
            "error": reason,
        })

        result = DownloadResult(
            url=url,
            platform=platform,
            status="failed",
            reason=reason,
            extractor_chain=extractor_chain,
            slots_tried=slots_tried,
            job_id=job_id,
            started_at=started,
            finished_at=finished,
            error=reason,
        )
        save_job(result.model_dump(mode="json"))
        return result

    async def batch(self, urls: list[str], *, dest: Path, opts: dict | None = None) -> list[DownloadResult]:
        """Many URLs in parallel. Per-host semaphore prevents hammering."""
        opts = opts or {}
        concurrency = opts.get("concurrency", 3)
        sem = asyncio.Semaphore(concurrency)

        async def _one(u: str) -> DownloadResult:
            async with sem:
                return await self.download(u, dest=dest, opts=opts)

        tasks = [asyncio.create_task(_one(u)) for u in urls]
        return await asyncio.gather(*tasks, return_exceptions=False)

    def supported(self) -> list[str]:
        return self.registry.supported_platforms()

    async def resume(self, job_id: str) -> DownloadResult:
        """Resume a job — re-runs from scratch (downloaded bytes are idempotent)."""
        from avd.utils.state import get_job

        row = get_job(job_id)
        if not row:
            return DownloadResult(
                url="",
                platform="unknown",
                status="failed",
                reason="job_not_found",
                job_id=job_id,
            )
        return await self.download(row["url"], dest=Path(row["artifact_path"] or "./download"), opts={"job_id": job_id})

    async def replay(self, dlq_entry_id: str) -> DownloadResult:
        """Replay a DLQ entry."""
        from avd.utils.state import list_dlq

        entries = list_dlq(limit=200)
        for e in entries:
            if e["id"] == dlq_entry_id:
                return await self.download(e["url"], dest=Path("./download"), opts={})
        return DownloadResult(
            url="",
            platform="unknown",
            status="failed",
            reason="dlq_entry_not_found",
            job_id=dlq_entry_id,
        )
