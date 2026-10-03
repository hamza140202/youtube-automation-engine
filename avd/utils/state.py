"""SQLite jobs table for resume / replay."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from avd.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    platform TEXT,
    status TEXT,
    artifact_path TEXT,
    extractor_chain_json TEXT,
    slots_tried_json TEXT,
    verifier_report_json TEXT,
    truth_report_json TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS dlq (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    url TEXT NOT NULL,
    platform TEXT,
    slots_tried_json TEXT,
    error TEXT,
    created_at TEXT
);
"""

_init_done: bool = False


def _db_path() -> Path:
    p = get_settings().db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    global _init_done
    if _init_done:
        return
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    _init_done = True


def save_job(result: dict[str, Any]) -> None:
    """Save / update a job row from a dict (subset of DownloadResult)."""
    _init()
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO jobs (id, url, platform, status, artifact_path,
                extractor_chain_json, slots_tried_json, verifier_report_json,
                truth_report_json, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 url=excluded.url,
                 platform=excluded.platform,
                 status=excluded.status,
                 artifact_path=excluded.artifact_path,
                 extractor_chain_json=excluded.extractor_chain_json,
                 slots_tried_json=excluded.slots_tried_json,
                 verifier_report_json=excluded.verifier_report_json,
                 truth_report_json=excluded.truth_report_json,
                 error=excluded.error,
                 updated_at=excluded.updated_at
            """,
            (
                result["job_id"],
                result["url"],
                result.get("platform"),
                result.get("status"),
                str(result.get("artifact_path") or ""),
                json.dumps(result.get("extractor_chain") or []),
                json.dumps([s if isinstance(s, dict) else s.model_dump() for s in (result.get("slots_tried") or [])]),
                json.dumps(result.get("verifier_report") or {}),
                json.dumps(result.get("truth_report") or {}),
                result.get("error"),
                result.get("started_at", now),
                now,
            ),
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    _init()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return dict(row)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    _init()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def write_dlq(entry: dict[str, Any]) -> None:
    _init()
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO dlq (id, job_id, url, platform, slots_tried_json, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                entry["id"],
                entry.get("job_id"),
                entry["url"],
                entry.get("platform"),
                json.dumps(entry.get("slots_tried") or []),
                entry.get("error"),
                now,
            ),
        )


def list_dlq(limit: int = 50) -> list[dict[str, Any]]:
    _init()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM dlq ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
