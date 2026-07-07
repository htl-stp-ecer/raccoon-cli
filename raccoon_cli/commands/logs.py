"""Log viewer commands — browse, filter, and tail libstp logs.

Fetches logs from the connected Pi via the server API.
Use --local only when explicitly working with local log files.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from raccoon_cli.logs import (
    DEFAULT_LIST_LIMIT,
    LogEntry,
    LogRun,
    current_log_file,
    discover_log_files,
    find_log_dir,
    load_run_by_index,
    load_runs,
    parse_log_file,
)
from raccoon_cli.logs.cmd_trace import (
    load_cmd_trace,
    resolve_cmd_trace_path,
    run_window_us,
    slice_cmd_trace,
)
from raccoon_cli.logs.journal import (
    bundle_journal_units,
    collect_journals,
    journal_manifest_section,
    write_journal_file,
)

# ── Colour palette ──────────────────────────────────────────────────
_LEVEL_STYLES = {
    "TRACE": "dim",
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARN": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


def _level_style(level: str) -> str:
    lvl = (level or "").upper()
    # spdlog emits "warning"; our palette keys it as WARN.
    if lvl == "WARNING":
        lvl = "WARN"
    return _LEVEL_STYLES.get(lvl, "")


def _format_duration(secs: float) -> str:
    if secs < 60:
        return f"{secs:.1f}s"
    mins = int(secs // 60)
    s = secs % 60
    return f"{mins}m {s:.0f}s"


# ── Remote helpers ──────────────────────────────────────────────────


def _get_remote_context(console: Console) -> tuple:
    """Get (connection_state, project_uuid) for the connected Pi.

    Exits with an error if the Pi is not reachable or the project has no UUID.
    """
    from raccoon_cli.client.connection import get_connection_manager
    from raccoon_cli.project import load_project_config, find_project_root, ProjectError

    manager = get_connection_manager()

    # Auto-connect if not already connected
    if not manager.is_connected:
        try:
            project_root = find_project_root()
            project_config = manager.load_from_project(project_root)
            if project_config and project_config.pi_address:
                manager.connect_sync(
                    project_config.pi_address,
                    project_config.pi_port,
                    project_config.pi_user,
                )
            else:
                known_pis = manager.load_known_pis()
                if known_pis:
                    pi = known_pis[0]
                    manager.connect_sync(pi.get("address"), pi.get("port", 8421))
        except Exception:
            pass

    if not manager.is_connected:
        console.print("[red]Not connected to a Pi.[/red]")
        console.print(
            "[dim]Run [cyan]raccoon connect <PI_ADDRESS>[/cyan] first, "
            "or use [cyan]raccoon logs --local[/cyan] to read local files.[/dim]"
        )
        raise SystemExit(1)

    state = manager.state

    try:
        config = load_project_config()
    except ProjectError:
        console.print("[red]Not in a Raccoon project directory.[/red]")
        console.print("[dim]Use [cyan]raccoon logs --local --dir <path>[/cyan] to read local files.[/dim]")
        raise SystemExit(1)

    project_uuid = config.get("uuid")
    if not project_uuid:
        console.print("[red]Project has no UUID in raccoon.project.yml.[/red]")
        raise SystemExit(1)

    return state, project_uuid


def _append_location(line: Text, location: str, func: str) -> None:
    """Append the fixed-width ``file:line`` + ``func`` columns to *line*.

    Mirrors ``live_stream``'s source/func columns so the post-hoc viewer reads
    the same. ``func`` (``Class.method`` for Python logs) is only shown when the
    library provided one — legacy text logs leave it blank.
    """
    if location:
        line.append(f"{location:<30.30} ", style="dim cyan")
    else:
        line.append(f"{'':30} ", style="dim")
    if func:
        line.append(f"{func:<24.24} ", style="dim italic")


def _render_entry_from_dict(entry: dict) -> Text:
    """Render a log entry dict (from API response) as Rich Text."""
    level = entry.get("level", "")
    style = _level_style(level)
    line = Text()
    line.append(f"{entry.get('elapsed', 0):>9.3f}s ", style="dim")
    line.append(f"{level:<8} ", style=style)
    source = entry.get("source", "")
    line_no = entry.get("line", 0) or 0
    location = f"{source}:{line_no}" if source and line_no else source
    _append_location(line, location, entry.get("func", "") or "")
    line.append(entry.get("message", ""))
    return line


def _render_entry(entry: LogEntry) -> Text:
    """Render a single log entry as a Rich Text object."""
    style = _level_style(entry.level)
    line = Text()
    line.append(f"{entry.elapsed:>9.3f}s ", style="dim")
    line.append(f"{entry.level_upper:<8} ", style=style)
    _append_location(line, entry.location, entry.func)
    line.append(entry.message)
    return line


def _render_level_text(counts: dict) -> Text:
    """Render level counts as styled Text."""
    level_parts = Text()
    for lvl in ("INFO", "WARN", "ERROR", "CRITICAL", "DEBUG", "TRACE"):
        c = counts.get(lvl, 0)
        if c:
            if level_parts:
                level_parts.append(" ")
            label = lvl[:3] if lvl != "CRITICAL" else "CRT"
            level_parts.append(f"{label}:{c}", style=_level_style(lvl))
    return level_parts


# ── Local helpers ───────────────────────────────────────────────────


def _resolve_log_dir(ctx: click.Context, log_dir: Optional[str]) -> Path:
    """Resolve the log directory, or exit with an error."""
    console: Console = ctx.obj.get("console", Console())

    if log_dir:
        p = Path(log_dir)
        if not p.is_dir():
            console.print(f"[red]Log directory not found: {log_dir}[/red]")
            raise SystemExit(1)
        return p

    found = find_log_dir()
    if not found:
        console.print(
            "[red]No .raccoon/runs/ directory with runs found.[/red]\n"
            "[dim]Pass --dir explicitly.[/dim]"
        )
        raise SystemExit(1)
    return found


def _load_all_runs(log_dir: Path, limit: Optional[int] = None) -> List[LogRun]:
    """Load runs from JSONL log files (most recent = index 1).

    Each run is a single JSONL file; see ``logs.load_runs``. *limit* caps how
    many of the newest files are parsed (indices are unaffected — they always
    count from the newest run).
    """
    return load_runs(discover_log_files(log_dir), limit=limit)


def _filter_entries(
    entries: List[LogEntry],
    level: Optional[str],
    source: Optional[str],
    grep: Optional[str],
) -> List[LogEntry]:
    """Apply level, source, and grep filters to entries."""
    result = entries

    if level:
        lvl = level.upper()
        result = [e for e in result if e.level_upper == lvl]

    if source:
        src_lower = source.lower()
        result = [e for e in result if src_lower in e.source.lower()]

    if grep:
        pattern = re.compile(grep, re.IGNORECASE)
        result = [e for e in result if pattern.search(e.message)]

    return result


# ── Click command group ─────────────────────────────────────────────


@click.group(name="logs", invoke_without_command=True)
@click.option("--dir", "log_dir", default=None, help="Path to a local .raccoon/runs/ directory (implies --local).")
@click.option("-n", "--last", "count", type=int, default=None, help="Show last N runs.")
@click.option("-a", "--all", "show_all", is_flag=True, help="Also include legacy rotated log files (from before the per-run scheme).")
@click.option("--local", is_flag=True, help="Read local logs instead of fetching from Pi.")
@click.pass_context
def logs_group(
    ctx: click.Context,
    log_dir: Optional[str],
    count: Optional[int],
    show_all: bool,
    local: bool,
) -> None:
    """Browse and inspect libstp log runs.

    Fetches logs from the connected Pi by default.
    Use --local to read logs from a local directory.
    """
    ctx.ensure_object(dict)
    ctx.obj["log_dir_override"] = log_dir
    ctx.obj["show_all"] = show_all
    ctx.obj["force_local"] = local or bool(log_dir)

    if ctx.invoked_subcommand is not None:
        return

    # Default action: list runs
    console: Console = ctx.obj.get("console", Console())

    if ctx.obj["force_local"]:
        _list_runs_local(ctx, console, log_dir, show_all, count)
    else:
        remote = _get_remote_context(console)
        asyncio.run(_list_runs_remote(console, remote, show_all, count))


def _list_runs_local(
    ctx: click.Context, console: Console, log_dir: Optional[str], show_all: bool, count: Optional[int],
) -> None:
    resolved = _resolve_log_dir(ctx, log_dir)
    # Parsing every line of every run file is slow, and the list only needs a
    # summary — so parse just the newest files (an explicit -n, or the default
    # cap). Older runs stay reachable by explicit index (e.g. `logs show 40`).
    total_files = len(discover_log_files(resolved))
    parse_limit = count if count else DEFAULT_LIST_LIMIT
    runs = _load_all_runs(resolved, limit=parse_limit)

    if not runs:
        console.print("[dim]No log runs found.[/dim]")
        return

    if count:
        runs = sorted(runs, key=lambda r: r.index)[:count]

    _render_run_table_local(console, runs, str(resolved), total_runs=total_files)


async def _list_runs_remote(
    console: Console, remote: tuple, show_all: bool, count: Optional[int],
) -> None:
    """Fetch and render run list from the Pi."""
    from raccoon_cli.client.api import create_api_client

    import httpx

    state, project_uuid = remote
    console.print(f"[dim]Fetching logs from {state.pi_hostname or state.pi_address}...[/dim]")

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            data = await client.list_log_runs(project_uuid, include_rotated=show_all, count=count)
        except httpx.TimeoutException:
            console.print(
                f"[red]Timed out waiting for the log list from "
                f"{state.pi_hostname or state.pi_address}.[/red]"
            )
            console.print(
                "[dim]The Pi took too long to enumerate its log runs. Try a smaller "
                "'-n <count>', or check the raccoon-server logs on the Pi.[/dim]"
            )
            raise SystemExit(1)
        except Exception as e:
            console.print(f"[red]Failed to fetch log runs: {e}[/red]")
            raise SystemExit(1)

    runs = data.get("runs", [])
    if not runs:
        console.print("[dim]No log runs found on Pi.[/dim]")
        return

    log_dir_label = f"{state.pi_hostname or state.pi_address}:{data.get('log_dir', 'logs/')}"
    _render_run_table_from_dicts(console, runs, log_dir_label, total_runs=data.get("total_runs"))


def _render_run_table_local(
    console: Console, runs: List[LogRun], title: str, total_runs: Optional[int] = None,
) -> None:
    """Display a table of local LogRun objects."""
    table = Table(
        title=f"Log runs — {title}",
        show_header=True,
        title_style="bold",
        padding=(0, 1),
    )
    table.add_column("#", style="bold cyan", width=4, justify="right")
    table.add_column("Started", style="dim")
    table.add_column("Duration", justify="right")
    table.add_column("Lines", justify="right")
    table.add_column("Sources")
    table.add_column("Levels")

    for run in sorted(runs, key=lambda r: r.index):
        sources = run.sources
        src_str = ", ".join(sorted(sources)[:3])
        if len(sources) > 3:
            src_str += f" +{len(sources) - 3}"

        table.add_row(
            str(run.index),
            run.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            _format_duration(run.duration_secs),
            str(run.line_count),
            src_str,
            _render_level_text(run.level_counts),
        )

    console.print(table)
    _render_run_table_footer(console, len(runs), total_runs)


def _render_run_table_footer(console: Console, shown: int, total: Optional[int]) -> None:
    """Footer line for the run list; notes when older runs weren't loaded."""
    if total is not None and total > shown:
        console.print(
            f"\n[dim]  Showing the {shown} most recent of {total} run(s). "
            f"Use [bold]-n <N>[/bold] for more, or [bold]raccoon logs show <#>[/bold] "
            f"for an older run.[/dim]"
        )
    else:
        console.print(
            f"\n[dim]  {shown} run(s) found. Use [bold]raccoon logs show <#>[/bold] to view a run.[/dim]"
        )


def _render_run_table_from_dicts(
    console: Console, runs: list[dict], title: str, total_runs: Optional[int] = None,
) -> None:
    """Display a table of runs from API response dicts."""
    table = Table(
        title=f"Log runs — {title}",
        show_header=True,
        title_style="bold",
        padding=(0, 1),
    )
    table.add_column("#", style="bold cyan", width=4, justify="right")
    table.add_column("Started", style="dim")
    table.add_column("Duration", justify="right")
    table.add_column("Lines", justify="right")
    table.add_column("Sources")
    table.add_column("Levels")

    for run in runs:
        sources = run.get("sources", [])
        src_str = ", ".join(sources[:3])
        if len(sources) > 3:
            src_str += f" +{len(sources) - 3}"

        table.add_row(
            str(run["index"]),
            run["start_time"].replace("T", " ")[:19],
            _format_duration(run.get("duration_secs", 0)),
            str(run.get("line_count", 0)),
            src_str,
            _render_level_text(run.get("level_counts", {})),
        )

    console.print(table)
    _render_run_table_footer(console, len(runs), total_runs)


# ── show ────────────────────────────────────────────────────────────


@logs_group.command(name="show")
@click.argument("run_id", type=int, default=1)
@click.option("-l", "--level", default=None, help="Filter by level (info, warn, error, ...).")
@click.option("-s", "--source", default=None, help="Filter by source file substring.")
@click.option("-g", "--grep", "grep_pattern", default=None, help="Filter messages by regex.")
@click.option("--no-pager", is_flag=True, help="Don't use a pager for output.")
@click.pass_context
def show_cmd(
    ctx: click.Context,
    run_id: int,
    level: Optional[str],
    source: Optional[str],
    grep_pattern: Optional[str],
    no_pager: bool,
) -> None:
    """Show log entries for a specific run.

    RUN_ID is the run number from 'raccoon logs' (default: 1 = most recent).
    """
    console: Console = ctx.obj.get("console", Console())

    if ctx.obj.get("force_local"):
        _show_run_local(ctx, console, run_id, level, source, grep_pattern, no_pager)
    else:
        remote = _get_remote_context(console)
        asyncio.run(_show_run_remote(
            console, remote, run_id, level, source, grep_pattern, no_pager,
            ctx.obj.get("show_all", False),
        ))


def _show_run_local(
    ctx: click.Context,
    console: Console,
    run_id: int,
    level: Optional[str],
    source: Optional[str],
    grep_pattern: Optional[str],
    no_pager: bool,
) -> None:
    log_dir = _resolve_log_dir(ctx, ctx.obj.get("log_dir_override"))
    files = discover_log_files(log_dir)
    run = load_run_by_index(files, run_id)
    if run is None:
        console.print(f"[red]Run #{run_id} not found.[/red] Available: 1–{len(files)}")
        raise SystemExit(1)

    entries = _filter_entries(run.entries, level, source, grep_pattern)

    if not entries:
        console.print(f"[dim]No entries match the filters for run #{run_id}.[/dim]")
        return

    header = (
        f"Run #{run.index}  |  {run.start_time:%Y-%m-%d %H:%M:%S}  |  "
        f"{_format_duration(run.duration_secs)}  |  {run.line_count} lines"
    )
    if level or source or grep_pattern:
        header += f"  |  showing {len(entries)}/{run.line_count}"

    if no_pager:
        console.print(Panel(header, style="bold"))
        for entry in entries:
            console.print(_render_entry(entry))
    else:
        with console.pager(styles=True):
            console.print(Panel(header, style="bold"))
            for entry in entries:
                console.print(_render_entry(entry))


async def _show_run_remote(
    console: Console,
    remote: tuple,
    run_id: int,
    level: Optional[str],
    source: Optional[str],
    grep_pattern: Optional[str],
    no_pager: bool,
    show_all: bool,
) -> None:
    """Fetch and render a specific run from the Pi."""
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote
    console.print(f"[dim]Fetching run #{run_id} from {state.pi_hostname or state.pi_address}...[/dim]")

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            data = await client.get_log_run(
                project_uuid, run_id, level=level, source=source,
                grep=grep_pattern, include_rotated=show_all,
            )
        except Exception as e:
            console.print(f"[red]Failed to fetch run: {e}[/red]")
            raise SystemExit(1)

    run_info = data.get("run", {})
    entries = data.get("entries", [])

    if not entries:
        console.print(f"[dim]No entries match the filters for run #{run_id}.[/dim]")
        return

    start = run_info.get("start_time", "").replace("T", " ")[:19]
    duration = _format_duration(run_info.get("duration_secs", 0))
    total = run_info.get("line_count", len(entries))
    header = f"Run #{run_id}  |  {start}  |  {duration}  |  {total} lines"
    if level or source or grep_pattern:
        header += f"  |  showing {data.get('filtered_count', len(entries))}/{total}"

    if no_pager:
        console.print(Panel(header, style="bold"))
        for entry in entries:
            console.print(_render_entry_from_dict(entry))
    else:
        with console.pager(styles=True):
            console.print(Panel(header, style="bold"))
            for entry in entries:
                console.print(_render_entry_from_dict(entry))


# ── download ────────────────────────────────────────────────────────


def _bundle_output_dir(explicit: Optional[str], run_index: int, start_time: datetime) -> Path:
    """Resolve the directory to write a bundle into.

    Defaults to ``.raccoon/downloads/run<idx>_<YYYYmmdd-HHMMSS>/`` under the
    current project (or CWD), named from the run's start time so re-downloading
    the same run is stable and the folder self-documents which mission it holds.
    """
    if explicit:
        return Path(explicit)

    from raccoon_cli.project import find_project_root, ProjectError

    try:
        base = find_project_root()
    except ProjectError:
        base = Path.cwd()

    stamp = start_time.strftime("%Y%m%d-%H%M%S")
    return base / ".raccoon" / "downloads" / f"run{run_index}_{stamp}"


# Canonical artifacts a unified run dir may hold. Presence is discovered at
# download time; the bundle manifest records name/size/present for each (plus
# any extra files found, e.g. per-mission ``profile.<Mission>.json``).
_RUN_ARTIFACTS = (
    "libstp.jsonl", "localization.jsonl", "profile.json", "run.json", "sensors.mcap",
)


def _artifact_entries(sizes: dict[str, int]) -> list[dict]:
    """Manifest artifact list from a name→size map of files in the bundle.

    Canonical run artifacts always appear (present/absent); any extra present
    files (``profile.M050.json``, ``cmd_trace.jsonl``, …) are appended.
    """
    entries: list[dict] = []
    listed: set[str] = set()
    for name in _RUN_ARTIFACTS:
        entries.append(
            {"name": name, "size": sizes.get(name, 0), "present": name in sizes}
        )
        listed.add(name)
    for name in sorted(sizes):
        if name not in listed:
            entries.append({"name": name, "size": sizes[name], "present": True})
    return entries


def _cmd_trace_manifest_section(cmd_trace: dict) -> dict:
    """The ``cmd_trace`` block of a bundle manifest (no raw entries)."""
    return {
        "file": "cmd_trace.jsonl",
        "source_path": cmd_trace.get("path") or cmd_trace.get("source_path"),
        "available": cmd_trace.get("available", False),
        "total_lines": cmd_trace.get("total_lines", 0),
        "matched_lines": cmd_trace.get("matched_lines", 0),
        "window_start_us": cmd_trace.get("window_start_us"),
        "window_end_us": cmd_trace.get("window_end_us"),
        "pad_secs": cmd_trace.get("pad_secs"),
    }


def _write_cmd_trace_file(output_dir: Path, cmd_trace: dict) -> int:
    """Write the windowed cmd_trace slice to ``cmd_trace.jsonl``; return its size."""
    trace_path = output_dir / "cmd_trace.jsonl"
    with open(trace_path, "w", encoding="utf-8") as f:
        for entry in cmd_trace.get("entries", []):
            f.write(json.dumps(entry) + "\n")
    return trace_path.stat().st_size


def _print_bundle_summary(
    console: Console,
    output_dir: Path,
    run_meta: dict,
    artifacts: list[dict],
    cmd_trace: dict,
    journals: Optional[list[dict]] = None,
) -> None:
    """Print a per-file summary (name + size / absent) plus cmd_trace + journals."""
    start = str(run_meta.get("start_time", "")).replace("T", " ")[:19]
    run_id = run_meta.get("run_id") or ""
    header = (
        f"Run #{run_meta.get('index')}"
        + (f" ({run_id})" if run_id else "")
        + f"  |  {start}  |  "
        f"{_format_duration(run_meta.get('duration_secs', 0))}  |  "
        f"{run_meta.get('line_count', 0)} log lines"
    )
    console.print(Panel(header, style="bold", title="Downloaded bundle"))

    for art in artifacts:
        name = art.get("name")
        if name == "cmd_trace.jsonl" or (name or "").startswith("journal."):
            continue  # summarised separately below
        if art.get("present"):
            console.print(
                f"  [cyan]{output_dir / name}[/cyan] "
                f"[dim]({_human_size(art.get('size', 0))})[/dim]"
            )
        else:
            console.print(f"  [dim]{name} — not present[/dim]")

    available = cmd_trace.get("available")
    matched = cmd_trace.get("matched_lines", 0)
    total = cmd_trace.get("total_lines", 0)
    src = cmd_trace.get("path") or cmd_trace.get("source_path")
    if not available:
        console.print(
            f"  [yellow]cmd_trace.jsonl not found[/yellow] "
            f"[dim]({src})[/dim] — wrote empty trace."
        )
    elif matched == 0:
        console.print(
            f"  [yellow]0 of {total} cmd_trace lines[/yellow] fell in the run "
            f"window [dim](reader likely restarted after the run)[/dim]."
        )
    else:
        console.print(
            f"  [cyan]{output_dir / 'cmd_trace.jsonl'}[/cyan] "
            f"[dim]({matched} of {total} cmd_trace lines in window)[/dim]"
        )

    for section in journals or []:
        _print_journal_line(console, output_dir, section)

    console.print(f"\n[green]Bundle written to[/green] [bold]{output_dir}[/bold]")


def _print_journal_line(console: Console, output_dir: Path, section: dict) -> None:
    """One summary line for a bundled service journal."""
    label = section.get("label", section.get("unit", "service"))
    file = section.get("file", "")
    count = section.get("entry_count", 0)
    if not section.get("available", False):
        err = section.get("error") or "unavailable"
        console.print(
            f"  [yellow]journal {label} unavailable[/yellow] [dim]({err})[/dim]"
        )
    elif count == 0:
        console.print(
            f"  [dim]journal {label} — no entries in window[/dim] "
            f"[dim]({output_dir / file})[/dim]"
        )
    else:
        console.print(
            f"  [cyan]{output_dir / file}[/cyan] "
            f"[dim]({count} {label} journal lines in window)[/dim]"
        )


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


@logs_group.command(name="download")
@click.argument("run_id", type=int, default=1)
@click.option("-o", "--output", "output_dir", default=None, help="Directory to write the bundle into (default: .raccoon/downloads/run<#>_<time>/).")
@click.option("--pad", "pad_secs", type=float, default=2.0, show_default=True, help="Seconds of cmd_trace padding around the run's time window.")
@click.option("--cmd-trace", "cmd_trace_path", default=None, help="[--local only] Path to cmd_trace.jsonl (default: reader's WOMBAT_CMD_TRACE / packaged path).")
@click.pass_context
def download_cmd(
    ctx: click.Context,
    run_id: int,
    output_dir: Optional[str],
    pad_secs: float,
    cmd_trace_path: Optional[str],
) -> None:
    """Download a run's logs + the STM32 cmd_trace slice for that timeframe.

    Bundles the selected run's raw libstp log file together with the
    stm32-data-reader command trace (cmd_trace.jsonl) and the journald output of
    every service raccoon manages — the raccoon-server, the stm32-data-reader,
    and each service declared in raccoon.project.yml — all filtered to the run's
    wall-clock time window, so a mission can be reconstructed offline for
    debugging.

    RUN_ID is the run number from 'raccoon logs' (default: 1 = most recent).
    """
    console: Console = ctx.obj.get("console", Console())

    if ctx.obj.get("force_local"):
        _download_local(ctx, console, run_id, output_dir, pad_secs, cmd_trace_path)
    else:
        if cmd_trace_path:
            console.print("[yellow]--cmd-trace is only used with --local; ignoring.[/yellow]")
        remote = _get_remote_context(console)
        asyncio.run(_download_remote(console, remote, run_id, output_dir, pad_secs))


def _download_local(
    ctx: click.Context,
    console: Console,
    run_id: int,
    output_dir: Optional[str],
    pad_secs: float,
    cmd_trace_path: Optional[str],
) -> None:
    log_dir = _resolve_log_dir(ctx, ctx.obj.get("log_dir_override"))
    files = discover_log_files(log_dir)
    run = load_run_by_index(files, run_id)
    if run is None:
        console.print(f"[red]Run #{run_id} not found.[/red] Available: 1–{len(files)}")
        raise SystemExit(1)

    if not run.run_dir:
        console.print(
            f"[red]Run #{run_id} has no unified run directory "
            f"(.raccoon/runs/<run_id>/) to download.[/red]"
        )
        raise SystemExit(1)
    run_dir = Path(run.run_dir)

    cmd_trace = _build_cmd_trace_slice(run, pad_secs, cmd_trace_path)
    journals = _build_journals_local(run, pad_secs)

    run_meta = {
        "index": run.index,
        "run_id": run.run_id,
        "start_time": run.start_time.isoformat(),
        "end_time": run.end_time.isoformat(),
        "duration_secs": run.duration_secs,
        "line_count": run.line_count,
    }

    out = _bundle_output_dir(output_dir, run.index, run.start_time)
    out.mkdir(parents=True, exist_ok=True)

    # Copy every artifact present in the run dir, then add the cmd_trace slice
    # and the windowed service journals.
    sizes: dict[str, int] = {}
    for f in sorted(run_dir.iterdir()):
        # Skip hidden sidecars (e.g. the .libstp.jsonl.meta.json summary cache).
        if f.is_file() and not f.name.startswith("."):
            dest = out / f.name
            shutil.copy2(f, dest)
            sizes[f.name] = dest.stat().st_size
    sizes["cmd_trace.jsonl"] = _write_cmd_trace_file(out, cmd_trace)
    for section in journals:
        sizes[section["file"]] = write_journal_file(out, section)

    artifacts = _artifact_entries(sizes)
    manifest = {
        "run": run_meta,
        "artifacts": artifacts,
        "cmd_trace": _cmd_trace_manifest_section(cmd_trace),
        "journals": [journal_manifest_section(s) for s in journals],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _print_bundle_summary(console, out, run_meta, artifacts, cmd_trace, journals)


def _build_cmd_trace_slice(run, pad_secs: float, cmd_trace_path: Optional[str]) -> dict:
    """Load and window the STM32 cmd_trace to *run*'s wall-clock time window."""
    start_us, end_us = run_window_us(run.start_time, run.end_time, pad_secs)
    trace_path = Path(cmd_trace_path) if cmd_trace_path else resolve_cmd_trace_path()
    cmd_trace: dict = {
        "path": str(trace_path),
        "available": False,
        "total_lines": 0,
        "matched_lines": 0,
        "window_start_us": start_us,
        "window_end_us": end_us,
        "pad_secs": pad_secs,
        "entries": [],
    }
    if trace_path.is_file():
        records = load_cmd_trace(trace_path)
        matched = slice_cmd_trace(records, start_us, end_us)
        cmd_trace.update(
            available=True,
            total_lines=len(records),
            matched_lines=len(matched),
            entries=matched,
        )
    return cmd_trace


def _build_journals_local(run, pad_secs: float) -> list[dict]:
    """Collect windowed journald slices for raccoon-managed services.

    Only meaningful when downloading on the Pi itself (``--local`` there) — off
    the robot journalctl won't know these units and each section is simply
    recorded as unavailable, keeping the bundle self-documenting.
    """
    from raccoon_cli.project import find_project_root, ProjectError

    try:
        project_path = find_project_root()
    except ProjectError:
        project_path = None

    start_us, end_us = run_window_us(run.start_time, run.end_time, pad_secs)
    units = bundle_journal_units(project_path)
    return collect_journals(units, start_us, end_us)


async def _download_remote(
    console: Console,
    remote: tuple,
    run_id: int,
    output_dir: Optional[str],
    pad_secs: float,
) -> None:
    """Fetch and write a run bundle from the Pi."""
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote
    console.print(
        f"[dim]Downloading run #{run_id} bundle from "
        f"{state.pi_hostname or state.pi_address}...[/dim]"
    )

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            zip_bytes = await client.download_log_bundle(project_uuid, run_id, pad_secs)
        except Exception as e:
            console.print(f"[red]Failed to download run: {e}[/red]")
            raise SystemExit(1)

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        console.print("[red]Server returned an invalid bundle (not a zip).[/red]")
        raise SystemExit(1)

    names = zf.namelist()
    manifest = json.loads(zf.read("manifest.json")) if "manifest.json" in names else {}
    run_meta = manifest.get("run", {})
    try:
        start_dt = datetime.fromisoformat(run_meta.get("start_time", ""))
    except ValueError:
        start_dt = datetime.now()

    out = _bundle_output_dir(output_dir, run_meta.get("index", run_id), start_dt)
    out.mkdir(parents=True, exist_ok=True)
    zf.extractall(out)

    # Prefer the manifest the server built; fall back to what actually landed.
    artifacts = manifest.get("artifacts")
    if not artifacts:
        sizes = {
            p.name: p.stat().st_size
            for p in out.iterdir()
            if p.is_file() and p.name != "manifest.json"
        }
        artifacts = _artifact_entries(sizes)
    _print_bundle_summary(
        console,
        out,
        run_meta,
        artifacts,
        manifest.get("cmd_trace", {}),
        manifest.get("journals", []),
    )


# ── tail ────────────────────────────────────────────────────────────


@logs_group.command(name="tail")
@click.option("-n", "--lines", "num_lines", type=int, default=20, help="Number of lines to show initially.")
@click.option("-f", "--follow", is_flag=True, help="Follow the log file for new output (requires --local).")
@click.option("-l", "--level", default=None, help="Filter by level.")
@click.option("-s", "--source", default=None, help="Filter by source file substring.")
@click.option("-g", "--grep", "grep_pattern", default=None, help="Filter messages by regex.")
@click.pass_context
def tail_cmd(
    ctx: click.Context,
    num_lines: int,
    follow: bool,
    level: Optional[str],
    source: Optional[str],
    grep_pattern: Optional[str],
) -> None:
    """Show the most recent log lines, optionally following for new output.

    Use -f to continuously watch for new log entries (requires --local).
    """
    console: Console = ctx.obj.get("console", Console())

    if follow and not ctx.obj.get("force_local"):
        console.print("[yellow]--follow requires local log access.[/yellow]")
        console.print("[dim]Use [cyan]raccoon logs --local tail -f[/cyan] when running on the Pi.[/dim]")
        raise SystemExit(1)

    if ctx.obj.get("force_local"):
        _tail_local(ctx, console, num_lines, follow, level, source, grep_pattern)
    else:
        remote = _get_remote_context(console)
        asyncio.run(_tail_remote(console, remote, num_lines, level, source, grep_pattern))


def _tail_local(
    ctx: click.Context,
    console: Console,
    num_lines: int,
    follow: bool,
    level: Optional[str],
    source: Optional[str],
    grep_pattern: Optional[str],
) -> None:
    log_dir = _resolve_log_dir(ctx, ctx.obj.get("log_dir_override"))
    log_file = current_log_file(log_dir)

    if log_file is None:
        console.print("[red]No libstp log files found.[/red]")
        raise SystemExit(1)

    all_entries = parse_log_file(log_file)
    filtered = _filter_entries(all_entries, level, source, grep_pattern)
    tail_entries = filtered[-num_lines:]

    for entry in tail_entries:
        console.print(_render_entry(entry))

    if not follow:
        return

    console.print("[dim]── following (Ctrl+C to stop) ──[/dim]")
    last_size = log_file.stat().st_size
    last_count = len(all_entries)

    try:
        while True:
            time.sleep(0.3)

            # A new run writes a *new* dated file — switch to it so `-f` keeps
            # following the live mission instead of the finished run's file.
            newest = current_log_file(log_dir)
            if newest is not None and newest != log_file:
                log_file = newest
                console.print(f"[dim]── new run: {log_file.name} ──[/dim]")
                last_size = 0
                last_count = 0

            current_size = log_file.stat().st_size

            if current_size == last_size:
                continue

            if current_size < last_size:
                last_count = 0

            all_entries = parse_log_file(log_file)
            new_entries = all_entries[last_count:]
            new_filtered = _filter_entries(new_entries, level, source, grep_pattern)

            for entry in new_filtered:
                console.print(_render_entry(entry))

            last_size = current_size
            last_count = len(all_entries)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


async def _tail_remote(
    console: Console,
    remote: tuple,
    num_lines: int,
    level: Optional[str],
    source: Optional[str],
    grep_pattern: Optional[str],
) -> None:
    """Fetch the last N lines of the most recent run from the Pi."""
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        data = await client.get_log_run(
            project_uuid, 1, level=level, source=source, grep=grep_pattern,
        )

    entries = data.get("entries", [])
    tail_entries = entries[-num_lines:]

    for entry in tail_entries:
        console.print(_render_entry_from_dict(entry))


# ── sources ─────────────────────────────────────────────────────────


@logs_group.command(name="sources")
@click.argument("run_id", type=int, default=1)
@click.pass_context
def sources_cmd(ctx: click.Context, run_id: int) -> None:
    """List all log sources (files) seen in a run."""
    console: Console = ctx.obj.get("console", Console())

    if ctx.obj.get("force_local"):
        _sources_local(ctx, console, run_id)
    else:
        remote = _get_remote_context(console)
        asyncio.run(_sources_remote(console, remote, run_id, ctx.obj.get("show_all", False)))


def _sources_local(ctx: click.Context, console: Console, run_id: int) -> None:
    log_dir = _resolve_log_dir(ctx, ctx.obj.get("log_dir_override"))
    files = discover_log_files(log_dir)
    run = load_run_by_index(files, run_id)
    if run is None:
        console.print(f"[red]Run #{run_id} not found.[/red]")
        raise SystemExit(1)

    _render_sources_from_entries(console, run_id, run.entries)


async def _sources_remote(console: Console, remote: tuple, run_id: int, show_all: bool) -> None:
    """Fetch a run from the Pi and display source breakdown."""
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            data = await client.get_log_run(project_uuid, run_id, include_rotated=show_all)
        except Exception as e:
            console.print(f"[red]Failed to fetch run: {e}[/red]")
            raise SystemExit(1)

    entries = data.get("entries", [])
    _render_sources_from_entry_dicts(console, run_id, entries)


def _render_sources_from_entries(console: Console, run_id: int, entries: List[LogEntry]) -> None:
    source_counts: dict[str, dict[str, int]] = {}
    for entry in entries:
        src = entry.source or "(none)"
        if src not in source_counts:
            source_counts[src] = {}
        lvl = entry.level_upper
        source_counts[src][lvl] = source_counts[src].get(lvl, 0) + 1

    _render_sources_table(console, run_id, source_counts)


def _render_sources_from_entry_dicts(console: Console, run_id: int, entries: list[dict]) -> None:
    source_counts: dict[str, dict[str, int]] = {}
    for entry in entries:
        src = entry.get("source", "") or "(none)"
        if src not in source_counts:
            source_counts[src] = {}
        lvl = entry.get("level", "INFO")
        source_counts[src][lvl] = source_counts[src].get(lvl, 0) + 1

    _render_sources_table(console, run_id, source_counts)


def _render_sources_table(console: Console, run_id: int, source_counts: dict[str, dict[str, int]]) -> None:
    table = Table(title=f"Sources in run #{run_id}", show_header=True)
    table.add_column("Source", style="cyan")
    table.add_column("Lines", justify="right")
    table.add_column("Levels")

    for src in sorted(source_counts):
        counts = source_counts[src]
        total = sum(counts.values())
        table.add_row(src, str(total), _render_level_text(counts))

    console.print(table)


# ── clear ───────────────────────────────────────────────────────────


@logs_group.command(name="clear")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@click.pass_context
def clear_cmd(ctx: click.Context, yes: bool) -> None:
    """Delete all log files (on Pi or locally with --local)."""
    console: Console = ctx.obj.get("console", Console())

    if ctx.obj.get("force_local"):
        _clear_local(ctx, console, yes)
    else:
        remote = _get_remote_context(console)
        if not yes:
            state = remote[0]
            if not click.confirm(f"Delete all logs on {state.pi_hostname or state.pi_address}?"):
                console.print("[dim]Cancelled.[/dim]")
                return
        asyncio.run(_clear_remote(console, remote))


def _clear_local(ctx: click.Context, console: Console, yes: bool) -> None:
    log_dir = _resolve_log_dir(ctx, ctx.obj.get("log_dir_override"))

    files = discover_log_files(log_dir)
    if not files:
        console.print("[dim]No log files to clear.[/dim]")
        return

    total_size = sum(f.stat().st_size for f in files)
    size_mb = total_size / (1024 * 1024)

    if not yes:
        console.print(f"About to delete {len(files)} log file(s) ({size_mb:.1f} MB):")
        for f in files:
            console.print(f"  [dim]{f.name}[/dim]")
        if not click.confirm("Continue?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    for f in files:
        f.unlink()

    timing_db = log_dir / "step_timing.db"
    if timing_db.exists():
        timing_db.unlink()
        console.print(f"[green]Deleted {len(files)} log file(s) + step_timing.db ({size_mb:.1f} MB).[/green]")
    else:
        console.print(f"[green]Deleted {len(files)} log file(s) ({size_mb:.1f} MB).[/green]")


# ── services ────────────────────────────────────────────────────────


_ACTIVE_STATE_STYLES = {
    "active": "bold green",
    "activating": "yellow",
    "reloading": "yellow",
    "inactive": "dim",
    "deactivating": "yellow",
    "failed": "bold red",
}


@logs_group.group(name="services", invoke_without_command=True)
@click.pass_context
def services_cmd(ctx: click.Context) -> None:
    """List project services and their journald output.

    Only works against the connected Pi — project services are systemd units
    on the robot.
    """
    if ctx.obj.get("force_local"):
        console: Console = ctx.obj.get("console", Console())
        console.print("[yellow]`raccoon logs services` is remote-only (services run on the Pi).[/yellow]")
        raise SystemExit(1)

    if ctx.invoked_subcommand is not None:
        return

    console: Console = ctx.obj.get("console", Console())
    remote = _get_remote_context(console)
    asyncio.run(_list_services_remote(console, remote))


async def _list_services_remote(console: Console, remote: tuple) -> None:
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote
    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            data = await client.list_project_services(project_uuid)
        except Exception as e:
            console.print(f"[red]Failed to fetch services: {e}[/red]")
            raise SystemExit(1)

    services = data.get("services", [])
    if not services:
        console.print(
            "[dim]No services declared in raccoon.project.yml.[/dim]\n"
            "[dim]See the [cyan]services:[/cyan] section in CLAUDE.md for the schema.[/dim]"
        )
        return

    table = Table(
        title=f"Project services on {state.pi_hostname or state.pi_address}",
        show_header=True,
        title_style="bold",
        padding=(0, 1),
    )
    table.add_column("Name", style="cyan")
    table.add_column("State")
    table.add_column("Sub", style="dim")
    table.add_column("PID", justify="right", style="dim")
    table.add_column("Restarts", justify="right")
    table.add_column("Started", style="dim")
    table.add_column("Required")

    for svc in services:
        active = svc.get("active_state", "unknown")
        style = _ACTIVE_STATE_STYLES.get(active, "")
        n_restarts = svc.get("n_restarts", "0")
        restart_style = "yellow" if n_restarts not in ("0", "", "—") else ""
        table.add_row(
            svc.get("name", "?"),
            Text(active, style=style),
            svc.get("sub_state", ""),
            svc.get("main_pid", "0"),
            Text(n_restarts, style=restart_style),
            (svc.get("active_enter_ts") or "")[:25],
            "yes" if svc.get("required_for_run") else "no",
        )

    console.print(table)
    console.print(
        f"\n[dim]  Use [bold]raccoon logs services <name>[/bold] to view journal output.[/dim]"
    )


@services_cmd.command(name="show")
@click.argument("service_name")
@click.option("-n", "--lines", type=int, default=200, help="Number of journal lines to fetch.")
@click.option("--no-pager", is_flag=True, help="Don't use a pager for output.")
@click.pass_context
def services_show_cmd(
    ctx: click.Context, service_name: str, lines: int, no_pager: bool
) -> None:
    """Show the last N journald entries for a project service."""
    console: Console = ctx.obj.get("console", Console())
    if ctx.obj.get("force_local"):
        console.print("[yellow]`raccoon logs services show` is remote-only.[/yellow]")
        raise SystemExit(1)

    remote = _get_remote_context(console)
    asyncio.run(_show_service_journal_remote(console, remote, service_name, lines, no_pager))


def _render_journal_entry(entry: dict) -> Text:
    level = entry.get("level", "INFO")
    style = _level_style(level)
    line = Text()
    ts = (entry.get("timestamp") or "").replace("T", " ")[:19]
    line.append(f"{ts} ", style="dim")
    line.append(f"{level:<6} ", style=style)
    line.append(entry.get("message", ""))
    return line


async def _show_service_journal_remote(
    console: Console,
    remote: tuple,
    service_name: str,
    lines: int,
    no_pager: bool,
) -> None:
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote
    console.print(
        f"[dim]Fetching journal for service '{service_name}' from "
        f"{state.pi_hostname or state.pi_address}...[/dim]"
    )

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            data = await client.get_service_journal(project_uuid, service_name, lines=lines)
        except Exception as e:
            console.print(f"[red]Failed to fetch journal: {e}[/red]")
            raise SystemExit(1)

    entries = data.get("entries", [])
    svc = data.get("service", {})
    header = (
        f"{svc.get('name', service_name)}  ({svc.get('systemd_name', '')})  "
        f"|  last {len(entries)} entries"
    )
    if not entries:
        console.print(Panel(header, style="bold"))
        console.print("[dim]No journal entries.[/dim]")
        return

    if no_pager:
        console.print(Panel(header, style="bold"))
        for entry in entries:
            console.print(_render_journal_entry(entry))
    else:
        with console.pager(styles=True):
            console.print(Panel(header, style="bold"))
            for entry in entries:
                console.print(_render_journal_entry(entry))


async def _clear_remote(console: Console, remote: tuple) -> None:
    """Delete logs on the Pi."""
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            data = await client.clear_logs(project_uuid)
        except Exception as e:
            console.print(f"[red]Failed to clear logs: {e}[/red]")
            raise SystemExit(1)

    deleted = data.get("deleted_files", 0)
    total_bytes = data.get("total_bytes", 0)
    size_mb = total_bytes / (1024 * 1024)
    console.print(f"[green]Deleted {deleted} log file(s) ({size_mb:.1f} MB) on {state.pi_hostname or state.pi_address}.[/green]")
