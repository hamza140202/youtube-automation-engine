"""Pydantic data models for all inter-agent payloads.

Every payload that crosses an agent boundary is a pydantic model. No bare dicts.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Type aliases ---
Platform = Literal["tiktok", "twitter", "reddit", "instagram", "rednote", "douyin", "unknown"]
Status = Literal["ok", "empty", "failed"]
TruthVerdict = Literal["verified", "suspicious", "unverifiable", "skipped"]
SlotOutcome = Literal["ok", "verifier_rejected", "extractor_failed", "skipped", "circuit_open"]


# --- Extractor metadata ---
class ExtractorMeta(BaseModel):
    """Declared by every Extractor implementation."""

    name: str
    priority: int = 100  # lower = tried first
    url_patterns: list[str] = Field(default_factory=list)
    platforms: list[Platform] = Field(default_factory=list)
    requires_network: bool = True
    description: str = ""


# --- Download metadata (returned by extractor) ---
class DownloadMetadata(BaseModel):
    """Metadata about the downloaded content."""

    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    duration_s: float | None = None
    thumbnail_url: str | None = None
    source_platform_post_id: str | None = None
    media_count: int | None = None
    codec_video: str | None = None
    codec_audio: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# --- Extractor outcome (Extractor -> Orchestrator) ---
class ExtractOutcome(BaseModel):
    """What an extractor returns to the orchestrator."""

    ok: bool
    artifact_path: Path | None = None
    metadata: DownloadMetadata = Field(default_factory=DownloadMetadata)
    extractor_name: str
    error: str | None = None
    raw_info: dict[str, Any] | None = None  # for debugging


# --- Verifier report ---
class VerifierReport(BaseModel):
    """6-layer integrity check result."""

    artifact_path: Path
    exists: bool = False
    size_bytes: int = 0
    mime_type: str | None = None
    has_video_stream: bool = False
    has_audio_stream: bool = False
    duration_s: float | None = None
    codec_video: str | None = None
    codec_audio: str | None = None
    container: str | None = None
    integrity_ok: bool = False
    issues: list[str] = Field(default_factory=list)


# --- Truth report ---
class TruthReport(BaseModel):
    """Cross-check downloaded metadata against source platform."""

    url: str
    platform: Platform
    source_meta: dict[str, Any] = Field(default_factory=dict)
    downloaded_meta: dict[str, Any] = Field(default_factory=dict)
    matches: dict[str, bool] = Field(default_factory=dict)
    confidence: float = 0.0
    verdict: TruthVerdict = "unverifiable"


# --- Slot attempt (one extractor's try) ---
class SlotAttempt(BaseModel):
    """Record of one extractor's attempt in the fallback chain."""

    extractor_name: str
    outcome: SlotOutcome
    failure_signal: str | None = None
    duration_s: float = 0.0


# --- Final DownloadResult (Orchestrator -> caller) ---
class DownloadResult(BaseModel):
    """Final result returned to the caller."""

    url: str
    platform: Platform
    status: Status
    reason: str | None = None
    artifact_path: Path | None = None
    metadata: DownloadMetadata = Field(default_factory=DownloadMetadata)
    extractor_chain: list[str] = Field(default_factory=list)
    slots_tried: list[SlotAttempt] = Field(default_factory=list)
    verifier_report: VerifierReport | None = None
    truth_report: TruthReport | None = None
    error: str | None = None
    job_id: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None


# --- Tester report ---
class TestReport(BaseModel):
    """One URL's test result from the Tester agent."""

    platform: Platform
    sample_url: str
    extractor_used: str | None = None
    downloaded: bool = False
    verified: bool = False
    truth_checked: bool = False
    truth_verdict: TruthVerdict = "skipped"
    duration_s: float = 0.0
    artifact_path: Path | None = None
    error: str | None = None
