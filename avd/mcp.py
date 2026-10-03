"""MCP server (stdio JSON-RPC 2.0) — pure-stdlib implementation.

Exposes:
  - extract    (url, dest)    → DownloadResult
  - verify     (path)         → VerifierReport
  - truth      (url, meta)    → TruthReport
  - get_schema ()             → JSON Schema for DownloadResult
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _ok(msg_id: Any, result: Any) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n").encode()


def _err(msg_id: Any, code: int, message: str) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}) + "\n").encode()


def _list_tools() -> list[dict[str, Any]]:
    return [
        {"name": "extract", "description": "Download a video from a URL via the full agent pipeline", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "dest": {"type": "string"}}, "required": ["url"]}},
        {"name": "verify", "description": "Verify a downloaded file's integrity", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "truth", "description": "Cross-check downloaded metadata against source platform", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "meta_json": {"type": "string"}}, "required": ["url", "meta_json"]}},
        {"name": "get_schema", "description": "Return the JSON schema for a DownloadResult", "inputSchema": {"type": "object"}},
    ]


async def _handle_call(method: str, params: dict[str, Any]) -> Any:
    from avd.orchestrator import Orchestrator
    from avd.verifier import Verifier

    if method == "extract":
        url = params["url"]
        dest = params.get("dest", "./download")
        orch = Orchestrator()
        result = await orch.download(url, dest=Path(dest), opts={})
        return result.model_dump(mode="json")
    elif method == "verify":
        path = params["path"]
        v = Verifier()
        report = await v.verify(Path(path))
        return report.model_dump(mode="json")
    elif method == "truth":
        url = params["url"]
        meta = json.loads(params["meta_json"]) if isinstance(params.get("meta_json"), str) else params.get("meta_json", {})
        from avd.truth_agent import TruthAgent

        t = TruthAgent()
        report = await t.cross_check(url, meta)
        return report.model_dump(mode="json")
    elif method == "get_schema":
        from avd.models import DownloadResult

        return DownloadResult.model_json_schema()
    raise ValueError(f"unknown method: {method}")


async def _read_loop() -> None:
    """Read JSON-RPC requests line by line from stdin."""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.buffer.write(_err(None, -32700, f"parse error: {e}"))
            sys.stdout.buffer.flush()
            continue
        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        try:
            if method == "initialize":
                sys.stdout.buffer.write(_ok(msg_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}))
            elif method == "tools/list":
                sys.stdout.buffer.write(_ok(msg_id, {"tools": _list_tools()}))
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                result = await _handle_call(name, args)
                sys.stdout.buffer.write(_ok(msg_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}))
            elif method == "ping":
                sys.stdout.buffer.write(_ok(msg_id, {}))
            elif method == "shutdown":
                sys.stdout.buffer.write(_ok(msg_id, {}))
                break
            else:
                sys.stdout.buffer.write(_err(msg_id, -32601, f"method not found: {method}"))
        except Exception as e:
            sys.stdout.buffer.write(_err(msg_id, -32603, f"internal error: {type(e).__name__}: {e}"))
        sys.stdout.buffer.flush()


def serve() -> None:
    """Main MCP server entry — blocking."""
    try:
        asyncio.run(_read_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
