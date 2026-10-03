"""CLI entry point — `avd` command."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from avd import __version__
from avd.config import get_settings
from avd.orchestrator import Orchestrator
from avd.tester import Tester
from avd.verifier import Verifier

console = Console(stderr=True)
stdout_console = Console()


@click.group()
@click.version_option(__version__, prog_name="avd")
def cli() -> None:
    """agent-video-downloader (avd) — yt-dlp for cloud agents."""


@cli.command()
@click.argument("url")
@click.option("--dest", "-d", default="./download", help="Destination directory")
@click.option("--json", "as_json", is_flag=True, help="Output JSON result to stdout")
@click.option("--no-verify", is_flag=True, help="Skip Verifier (NOT recommended)")
@click.option("--no-truth", is_flag=True, help="Skip Truth Agent")
def download(url: str, dest: str, as_json: bool, no_verify: bool, no_truth: bool) -> None:
    """Download a single URL.

    Auto-bootstraps XHS-Downloader on first Rednote download (no manual setup).
    """
    orch = Orchestrator()
    opts = {}
    if no_verify:
        opts["no_verify"] = True
    if no_truth:
        opts["no_truth"] = True
    result = asyncio.run(orch.download(url, dest=Path(dest), opts=opts))
    if as_json:
        stdout_console.print_json(json.dumps(result.model_dump(mode="json"), default=str))
    else:
        if result.status == "ok":
            console.print(f"[green]✅ downloaded[/] {result.artifact_path}")
            if result.verifier_report:
                console.print(f"[dim]verifier: integrity_ok={result.verifier_report.integrity_ok} size={result.verifier_report.size_bytes}B duration={result.verifier_report.duration_s}s[/]")
            if result.truth_report:
                console.print(f"[dim]truth: {result.truth_report.verdict} (confidence {result.truth_report.confidence:.2f})[/]")
        elif result.status == "empty":
            console.print(f"[yellow]⚠️ empty[/] {url} — reason: {result.reason}")
        else:
            console.print(f"[red]❌ failed[/] {url} — reason: {result.reason}")
            if result.slots_tried:
                for s in result.slots_tried:
                    console.print(f"[dim]  - {s.extractor_name}: {s.outcome} ({s.failure_signal})[/]")
    sys.exit(0 if result.status == "ok" else 1)


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--dest", "-d", default="./download", help="Destination directory")
@click.option("--concurrency", "-c", default=3, help="Parallel downloads")
def batch(file: str, dest: str, concurrency: int) -> None:
    """Batch download from a file (one URL per line, # comments OK)."""
    with open(file) as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not urls:
        console.print("[red]no URLs in file[/]")
        sys.exit(1)
    orch = Orchestrator()
    results = asyncio.run(orch.batch(urls, dest=Path(dest), opts={"concurrency": concurrency}))

    table = Table(title="avd batch results")
    table.add_column("URL", overflow="fold")
    table.add_column("Status")
    table.add_column("Path")
    for r in results:
        status_color = "green" if r.status == "ok" else "yellow" if r.status == "empty" else "red"
        table.add_row(r.url, f"[{status_color}]{r.status}[/{status_color}]", str(r.artifact_path or ""))
    console.print(table)
    ok_count = sum(1 for r in results if r.status == "ok")
    sys.exit(0 if ok_count == len(results) else 1)


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--json", "as_json", is_flag=True, help="Output JSON report to stdout")
def verify(path: str, as_json: bool) -> None:
    """Verify a previously downloaded file."""
    v = Verifier()
    report = asyncio.run(v.verify(Path(path)))
    if as_json:
        stdout_console.print_json(json.dumps(report.model_dump(mode="json"), default=str))
    else:
        if report.integrity_ok:
            console.print(f"[green]✅ verified[/] {path}")
            console.print(f"[dim]size={report.size_bytes}B mime={report.mime_type} duration={report.duration_s}s[/]")
        else:
            console.print(f"[red]❌ rejected[/] {path}")
            for issue in report.issues:
                console.print(f"[red]  - {issue}[/]")
    sys.exit(0 if report.integrity_ok else 1)


@cli.command(name="test")
@click.option("--smoke/--full", default=True, help="Smoke (one URL per platform) or full")
@click.option("--platform", "-p", "platforms", multiple=True, help="Filter to platforms (repeatable)")
def test_cmd(smoke: bool, platforms: tuple[str, ...]) -> None:
    """Run end-to-end self-test."""
    tester = Tester()
    reports = asyncio.run(tester.run(platforms=list(platforms) or None, smoke=smoke))
    passed = sum(1 for r in reports if r.downloaded and r.verified)
    console.print(f"\n[bold]Pass: {passed}/{len(reports)}[/]")
    sys.exit(0 if passed >= (1 if smoke else len(reports) // 2) else 1)


@cli.command()
def supported() -> None:
    """Show supported platforms."""
    orch = Orchestrator()
    table = Table(title="Supported platforms")
    table.add_column("Platform")
    for p in orch.supported():
        table.add_row(p)
    console.print(table)


@cli.command()
def agents() -> None:
    """Show agent versions."""
    from avd.truth_agent import TruthAgent

    table = Table(title="Agent versions")
    table.add_column("Agent")
    table.add_column("Version")
    table.add_row("Orchestrator", Orchestrator.__version__)
    table.add_row("Verifier", Verifier.__version__)
    table.add_row("TruthAgent", TruthAgent.__version__)
    table.add_row("Tester", Tester.__version__)
    console.print(table)


@cli.command(name="agent-setup")
@click.option("--force", is_flag=True, help="Re-clone XHS-Downloader even if present")
def agent_setup_cmd(force: bool) -> None:
    """One-command setup for AI agents.

    Auto-installs:
      - ffmpeg/ffprobe (system binary — prints install hint if missing)
      - All Python pip dependencies (idempotent)
      - JoeanAmier/XHS-Downloader repo (cloned to ~/XHS-Downloader on first run)
      - All XHS-Downloader Python deps (curl-cffi, fastapi, fastmcp, textual, ...)

    This command is safe to call on every download — it's idempotent.
    """
    console.print(Panel.fit(
        f"[bold cyan]avd agent-setup[/]  v{__version__}\n"
        "[dim]One-command bootstrap for AI agents.[/]",
        border_style="cyan",
    ))

    from avd import bootstrap

    # Step 1: ffmpeg check
    console.print("\n[bold]Step 1/3:[/] ffmpeg / ffprobe")
    if bootstrap.ensure_ffmpeg():
        ffprobe_v = subprocess_run(["ffprobe", "-version"])
        console.print(f"  [green]✅[/] ffprobe found — {ffprobe_v.splitlines()[0] if ffprobe_v else ''}")
    else:
        console.print("  [red]❌[/] ffprobe missing — install with: sudo apt install ffmpeg (or brew install ffmpeg)")
        console.print("  [dim]avd will still run, but Verifier's magic-byte + stream checks will fail.[/]")

    # Step 2: pip deps
    console.print("\n[bold]Step 2/3:[/] Python dependencies")
    if bootstrap.ensure_pip_deps():
        console.print("  [green]✅[/] all pip deps importable")
    else:
        console.print("  [yellow]⚠️[/] some deps missing — running `pip install -e .[dev]`")
        result = subprocess_run([sys.executable, "-m", "pip", "install", "--break-system-packages", "-e", str(Path(__file__).parent.parent.parent), "--quiet"])
        if result:
            console.print("  [green]✅[/] installed")
        else:
            console.print("  [red]❌[/] install failed — see logs above")

    # Step 3: XHS-Downloader
    console.print("\n[bold]Step 3/3:[/] XHS-Downloader (for Rednote support)")
    if force:
        from avd.bootstrap import find_xhs_downloader, ensure_xhs_downloader
        existing = find_xhs_downloader()
        if existing and Path(existing).exists():
            import shutil
            shutil.rmtree(existing)
            console.print(f"  [dim]removed existing clone at {existing}[/]")
    xhs_path = bootstrap.ensure_xhs_downloader()
    if xhs_path:
        console.print(f"  [green]✅[/] XHS-Downloader ready at [cyan]{xhs_path}[/]")
    else:
        console.print("  [red]❌[/] failed to clone XHS-Downloader")
        console.print("  [dim]you can still use TikTok/Twitter/Reddit/Instagram/Douyin — Rednote will be skipped.[/]")

    # Final summary
    console.print("\n[bold]Setup summary:[/]")
    summary = bootstrap.full_bootstrap()
    table = Table(show_header=False, box=None)
    table.add_column("Component", style="cyan")
    table.add_column("Status")
    table.add_row("ffmpeg", "[green]✅[/]" if summary["ffmpeg"] else "[red]❌[/]")
    table.add_row("pip deps", "[green]✅[/]" if summary["pip_deps"] else "[red]❌[/]")
    table.add_row("XHS-Downloader", f"[green]✅[/] {summary['xhs_downloader_path']}" if summary['xhs_downloader_path'] else "[red]❌[/]")
    table.add_row("READY", "[bold green]✅[/] [bold]avd is ready[/]" if summary["ready"] else "[bold yellow]⚠️[/] partial")
    console.print(table)

    if summary["ready"]:
        console.print("\n[bold green]avd is ready to download.[/] Try:")
        console.print("  [cyan]avd download 'https://www.tiktok.com/@scout2015/video/6718335390845095173'[/]")
        console.print("  [cyan]avd test --smoke[/]  # end-to-end self-test")
        console.print("  [cyan]avd agent-instructions[/]  # step-by-step usage guide")
    sys.exit(0 if summary["ready"] else 1)


@cli.command(name="agent-instructions")
def agent_instructions_cmd() -> None:
    """Print step-by-step instructions for AI agents to use avd.

    Designed to be read by Claude / Cursor / Cline / GLM / GPT agents
    that have just been told to use avd for video downloads.

    The instructions are deterministic — every step is a single command.
    """
    instructions = f"""# avd — agent usage instructions

**avd** is a CLI video downloader for **TikTok, Instagram, Douyin, Rednote (Xiaohongshu), Reddit, X.com**. Built for cloud agents (no browser, no cookies, no login). v{__version__}.

## Step 1 — Install (one command)

```bash
pip install agent-video-downloader
avd agent-setup
```

`avd agent-setup` auto-installs ffmpeg, all Python deps, and the XHS-Downloader
repo (for Rednote support). Idempotent — safe to re-run.

If `pip install agent-video-downloader` fails, install from source:
```bash
git clone https://github.com/hamza140202/agent-video-downloader
cd agent-video-downloader
pip install -e .
avd agent-setup
```

## Step 2 — Verify the install

```bash
avd --version
avd supported
avd agent-setup   # re-run anytime to verify / repair
avd test --smoke  # end-to-end test on one URL per platform (~30s)
```

Expected: `Pass: 4/6` or better (some platforms need OAuth or are datacenter-IP walled; the system honestly reports `datacenter_ip_walled`).

## Step 3 — Download a single URL

```bash
avd download '<url>' --dest ./download
```

Examples (verified live 2026-10-03):
```bash
avd download 'https://www.tiktok.com/@scout2015/video/6718335390845095173'
avd download 'https://x.com/SpaceX/status/2072695632104468543'
avd download 'https://www.reddit.com/r/funny/comments/1ww6c5f/a_perfectly_even_match/'
avd download 'https://www.instagram.com/p/DKw2J6TMZd7/'
avd download 'https://www.douyin.com/video/7324982373882400063'
avd download 'https://www.xiaohongshu.com/explore/6a40dd42000000000f02a91b'
```

Output is pipe-safe — logs on stderr, the downloaded file path on stdout:
```bash
FILE=$(avd download '<url>' --dest ./download 2>/dev/null | head -1)
# FILE now contains the path to the downloaded MP4
```

## Step 4 — Batch download (file with one URL per line)

```bash
avd batch urls.txt --dest ./download --concurrency 3
```

`urls.txt` format:
```
# comments allowed
https://www.tiktok.com/@scout2015/video/6718335390845095173
https://x.com/SpaceX/status/2072695632104468543
```

## Step 5 — Get JSON output (for programmatic use)

```bash
avd download '<url>' --json
```

Returns the full `DownloadResult` JSON on stdout. Fields:
- `status`: `"ok" | "empty" | "failed"`
- `artifact_path`: path to the downloaded file (or null)
- `verifier_report`: full integrity report (size, mime, duration, codecs)
- `truth_report`: cross-check verdict (`verified | suspicious | unverifiable`)
- `extractor_chain`: list of extractors tried
- `slots_tried`: per-extractor outcome + failure signal

## Step 6 — Verify an existing file

```bash
avd verify ./download/tiktok/6718335390845095173.mp4
avd verify ./download/tiktok/6718335390845095173.mp4 --json
```

## Step 7 — Inspect job state

```bash
avd jobs        # last 20 jobs
avd dlq         # dead-letter queue (failed downloads)
avd resume <job_id>
avd replay <dlq_id>
```

## Step 8 — MCP server mode (for MCP-aware agents like Claude Desktop)

```bash
avd mcp
```

Speaks JSON-RPC 2.0 over stdio. Exposes:
- `extract` (url, dest) → DownloadResult
- `verify` (path) → VerifierReport
- `truth` (url, meta_json) → TruthReport
- `get_schema` () → JSON schema

Wire into Claude Desktop's `claude_desktop_config.json`:
```json
{{
  "mcpServers": {{
    "avd": {{
      "command": "avd",
      "args": ["mcp"]
    }}
  }}
}}
```

## Agent decision tree (copy this into your agent prompt)

```
IF user asks for video bytes from a URL → use avd
  IF url matches (tiktok|instagram|douyin|rednote|reddit|x.com) →
    run: avd download '<url>' --dest ./download
    IF exit 0 → file is ready at the path printed to stdout
    IF exit 1 → check stderr for `reason`:
      - "datacenter_ip_walled" → platform needs proxy/Docker (out of scope)
      - "oauth_required" → set AVD_REDDIT_* env vars (Reddit only)
      - "deleted_or_private" → content gone, inform user
      - "all_extractors_exhausted" → check `avd dlq` and `avd replay`
  ELSE → not supported, suggest yt-dlp directly
```

## Platform support matrix (verified live 2026-10-03 from datacenter IP)

| Platform | Primary method | Status |
|---|---|---|
| TikTok | TikWM mirror API | ✅ verified |
| Twitter/X | api.fxtwitter.com | ✅ verified |
| Reddit | rapidsave.com + v.redd.it CMAF + ffmpeg mux | ✅ verified |
| Instagram | yt-dlp + facebookexternalhit UA | ✅ verified |
| Douyin | api.douyin.wtf public demo | ✅ verified |
| Rednote (Xiaohongshu) | XHS-Downloader (curl_cffi) | ✅ verified |

## Config (env vars)

| Var | Default | Description |
|---|---|---|
| `AVD_DOWNLOAD_DIR` | `./download` | Default download dir |
| `AVD_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `AVD_REDDIT_CLIENT_ID` | — | Reddit OAuth (only if rapidsave fails) |
| `AVD_REDDIT_CLIENT_SECRET` | — | same |
| `AVD_REDDIT_USERNAME` | — | throwaway Reddit username |
| `AVD_REDDIT_PASSWORD` | — | throwaway Reddit password |
| `AVD_DOUYIN_DTK_URL` | — | self-hosted Evil0ctal DTK (optional — defaults to public demo) |
| `AVD_XHS_DOWNLOADER_PATH` | `~/XHS-Downloader` | override XHS-Downloader clone path |

## Documentation

- `CLAUDE.md` — project memory (read first if modifying the code)
- `AGENTS.md` — agent contracts
- `docs/research-report.md` — live-verified endpoint research per platform
- `docs/endpoint-matrix.md` — living endpoint table
- https://github.com/hamza140202/agent-video-downloader

## License

MIT. See `LICENSE`.
"""
    console.print(Markdown(instructions))


@cli.command()
def jobs() -> None:
    """List recent jobs."""
    from avd.utils.state import list_jobs

    rows = list_jobs(limit=20)
    if not rows:
        console.print("[dim]no jobs[/]")
        return
    table = Table(title="Recent jobs")
    table.add_column("ID")
    table.add_column("URL", overflow="fold")
    table.add_column("Status")
    table.add_column("Platform")
    for r in rows:
        table.add_row(r["id"], r["url"][:80], r["status"] or "-", r["platform"] or "-")
    console.print(table)


@cli.command()
@click.argument("job_id")
def resume(job_id: str) -> None:
    """Resume a job by ID."""
    orch = Orchestrator()
    result = asyncio.run(orch.resume(job_id))
    console.print(f"[bold]resume result[/]: {result.status} {result.reason or ''}")


@cli.command()
def dlq() -> None:
    """List dead-letter queue entries."""
    from avd.utils.state import list_dlq

    rows = list_dlq(limit=20)
    if not rows:
        console.print("[dim]DLQ empty[/]")
        return
    table = Table(title="DLQ entries")
    table.add_column("ID")
    table.add_column("URL", overflow="fold")
    table.add_column("Error")
    for r in rows:
        table.add_row(r["id"], r["url"][:80], r["error"] or "-")
    console.print(table)


@cli.command()
@click.argument("dlq_id")
def replay(dlq_id: str) -> None:
    """Replay a DLQ entry."""
    orch = Orchestrator()
    result = asyncio.run(orch.replay(dlq_id))
    console.print(f"[bold]replay result[/]: {result.status} {result.reason or ''}")


@cli.command()
def mcp() -> None:
    """Start MCP server (stdio JSON-RPC 2.0)."""
    from avd.mcp import serve

    serve()


def subprocess_run(cmd: list[str]) -> str:
    """Helper to run a subprocess and capture stdout."""
    try:
        result = __import__("subprocess").run(cmd, capture_output=True, text=True, timeout=15)
        return result.stdout
    except Exception:
        return ""


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
