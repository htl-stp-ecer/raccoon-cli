"""Log viewer commands — browse, filter, and tail libstp logs.

Fetches logs from the connected Pi via the server API.
Use --local only when explicitly working with local log files.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from raccoon_cli.logs import (
    LogEntry,
    LogRun,
    detect_runs,
    discover_log_files,
    find_log_dir,
    parse_log_file,
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
    return _LEVEL_STYLES.get(level.upper(), "")


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


def _render_entry_from_dict(entry: dict) -> Text:
    """Render a log entry dict (from API response) as Rich Text."""
    level = entry.get("level", "")
    style = _level_style(level)
    line = Text()
    line.append(f"{entry.get('elapsed', 0):>9.3f}s ", style="dim")
    line.append(f"{level:<8} ", style=style)
    source = entry.get("source", "")
    if source:
        line.append(f"{source:<30} ", style="dim cyan")
    else:
        line.append(f"{'':30} ", style="dim")
    line.append(entry.get("message", ""))
    return line


def _render_entry(entry: LogEntry) -> Text:
    """Render a single log entry as a Rich Text object."""
    style = _level_style(entry.level)
    line = Text()
    line.append(f"{entry.elapsed:>9.3f}s ", style="dim")
    line.append(f"{entry.level_upper:<8} ", style=style)
    if entry.source:
        line.append(f"{entry.source:<30} ", style="dim cyan")
    else:
        line.append(f"{'':30} ", style="dim")
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
            "[red]No .raccoon/logs/ directory found.[/red]\n"
            "[dim]Pass --dir explicitly.[/dim]"
        )
        raise SystemExit(1)
    return found


def _load_all_runs(log_dir: Path) -> List[LogRun]:
    """Parse all log files and return detected runs (most recent = index 1)."""
    files = discover_log_files(log_dir)
    all_entries: List[LogEntry] = []
    for f in files:
        all_entries.extend(parse_log_file(f))
    return detect_runs(all_entries)


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
@click.option("--dir", "log_dir", default=None, help="Path to a local .raccoon/logs/ directory (implies --local).")
@click.option("-n", "--last", "count", type=int, default=None, help="Show last N runs.")
@click.option("-a", "--all", "show_all", is_flag=True, help="Include rotated log files.")
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
    runs = _load_all_runs(resolved)

    if not runs:
        console.print("[dim]No log runs found.[/dim]")
        return

    if not show_all:
        current_file = str(resolved / "libstp.log")
        runs = [r for r in runs if r.file_path == current_file] or runs

    if count:
        runs = sorted(runs, key=lambda r: r.index)[:count]

    _render_run_table_local(console, runs, str(resolved))


async def _list_runs_remote(
    console: Console, remote: tuple, show_all: bool, count: Optional[int],
) -> None:
    """Fetch and render run list from the Pi."""
    from raccoon_cli.client.api import create_api_client

    state, project_uuid = remote
    console.print(f"[dim]Fetching logs from {state.pi_hostname or state.pi_address}...[/dim]")

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        data = await client.list_log_runs(project_uuid, include_rotated=show_all, count=count)

    runs = data.get("runs", [])
    if not runs:
        console.print("[dim]No log runs found on Pi.[/dim]")
        return

    log_dir_label = f"{state.pi_hostname or state.pi_address}:{data.get('log_dir', 'logs/')}"
    _render_run_table_from_dicts(console, runs, log_dir_label)


def _render_run_table_local(console: Console, runs: List[LogRun], title: str) -> None:
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
    console.print(
        f"\n[dim]  {len(runs)} run(s) found. Use [bold]raccoon logs show <#>[/bold] to view a run.[/dim]"
    )


def _render_run_table_from_dicts(console: Console, runs: list[dict], title: str) -> None:
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
    console.print(
        f"\n[dim]  {len(runs)} run(s) found. Use [bold]raccoon logs show <#>[/bold] to view a run.[/dim]"
    )


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
    runs = _load_all_runs(log_dir)

    run = next((r for r in runs if r.index == run_id), None)
    if run is None:
        console.print(f"[red]Run #{run_id} not found.[/red] Available: 1–{len(runs)}")
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
    log_file = log_dir / "libstp.log"

    if not log_file.exists():
        console.print("[red]No libstp.log found.[/red]")
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
    runs = _load_all_runs(log_dir)

    run = next((r for r in runs if r.index == run_id), None)
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
