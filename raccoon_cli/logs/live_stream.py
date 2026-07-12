"""Live TUI that streams a running program's JSONL log instead of raw stdout.

The library (raccoon-lib) writes one JSONL file per run at
``.raccoon/runs/<run_id>/libstp.jsonl`` — one JSON object per line with all
metadata (``t``, ``elapsed``, ``seq``, ``level``, ``logger``, ``thread``,
``pid``, ``file``, ``line``, ``func``, ``msg``). Its stdout now carries only
warn/error. So during ``raccoon run`` we suppress the child's stdout and render
the JSONL live in a scrolling, colourised viewport (à la ``journalctl -f``),
matching the look of ``raccoon logs``.

The public entry point is :func:`stream_run_logs`. Everything else
(:class:`LiveRecord`, :func:`follow_lines`, :class:`LiveLogView`) is reusable
and unit-tested independently of an actual robot process.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Iterator, Optional

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .parser import parse_jsonl_line
from .progress import RunProgress

# Phase → badge style for the breadcrumb, so the current stage of the run reads
# at a glance (setup warming up, main scoring, shutdown winding down).
_PHASE_STYLES = {
    "setup": "blue",
    "main": "bold magenta",
    "shutdown": "dim",
}

# One JSONL file per run; the zero-padded timestamp sorts chronologically.
JSONL_GLOB = "libstp-*.jsonl"

# Shared palette with `raccoon logs` (see commands/logs.py) so the live view and
# the post-hoc viewer read the same.
_LEVEL_STYLES = {
    "TRACE": "dim",
    "DEBUG": "cyan",
    "INFO": "green",
    "WARN": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}

# Levels that deserve a highlighted message, not just a coloured badge.
_LOUD = {"WARN", "ERROR", "CRITICAL"}

_LEVEL_ORDER = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]


# Body-display floor: hide trace/debug from the scrolling view by default (just
# elapsed/level/message of the meaningful INFO+ lines).
DEFAULT_MIN_LEVEL = "INFO"

# Network floor for remote runs: the Pi drops only TRACE (the bulk — a run is
# mostly trace), but keeps DEBUG on the wire so the debug-level mission-preload
# markers still reach the laptop and build the full breadcrumb. The laptop then
# hides DEBUG from the *display* via DEFAULT_MIN_LEVEL. Cheap and lossless for
# the breadcrumb.
DEFAULT_STREAM_LEVEL = "DEBUG"


def _level_style(level: str) -> str:
    return _LEVEL_STYLES.get(level.upper(), "")


def _norm_level(level: str) -> str:
    lvl = (level or "").upper()
    return "WARN" if lvl == "WARNING" else lvl


def level_rank(level: str) -> int:
    """Ordinal of *level* in :data:`_LEVEL_ORDER`; unknown levels rank highest.

    Unknown levels sort above CRITICAL so a filter never silently drops a level
    it doesn't recognise.
    """
    try:
        return _LEVEL_ORDER.index(_norm_level(level))
    except ValueError:
        return len(_LEVEL_ORDER)


def line_at_or_above(line: str, min_rank: int) -> bool:
    """True if a raw JSONL *line*'s level is at least *min_rank*.

    Non-JSON lines (a stray banner, a crash-tail line) return ``True`` — they're
    rare and cheap, and dropping them could hide something important. Used by the
    Pi-side streamer to avoid pushing trace/debug over the network.
    """
    entry = parse_jsonl_line(line)
    if entry is None:
        return True
    return level_rank(entry.level) >= min_rank


@dataclass
class LiveRecord:
    """A single parsed JSONL log record (the fields we render)."""

    elapsed: float
    level: str  # normalised upper-case (WARNING → WARN)
    file: str  # basename only
    line: int
    func: str
    message: str
    seq: int = 0

    @property
    def source(self) -> str:
        """``file:line`` (or just ``file``) — the emitting location."""
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file


def parse_record(line: str) -> Optional[LiveRecord]:
    """Parse one JSONL line into a :class:`LiveRecord`.

    Delegates decoding to :func:`raccoon_cli.logs.parser.parse_jsonl_line` — the
    single source of truth shared with the post-hoc ``raccoon logs`` viewer — and
    projects its richer :class:`~raccoon_cli.logs.parser.LogEntry` onto the light
    :class:`LiveRecord` the TUI renders. Returns ``None`` for blank lines or
    anything that isn't a JSON object, so a stray non-JSON line is skipped rather
    than crashing the stream.
    """
    entry = parse_jsonl_line(line)
    if entry is None:
        return None
    return LiveRecord(
        elapsed=entry.elapsed,
        level=_norm_level(entry.level),
        file=entry.source,  # basename — full path is in entry.file_path
        line=entry.line_number,
        func=entry.func,
        message=entry.message,
        seq=entry.seq,
    )


def newest_jsonl(runs_dir: Path) -> Optional[Path]:
    """Return the newest run's JSONL log under ``.raccoon/runs/``, or ``None``.

    *runs_dir* is the ``.raccoon/runs/`` directory; the newest run dir's
    ``libstp.jsonl`` wins — so a standalone live view still finds the current run.
    """
    from .finder import discover_log_files

    candidates = discover_log_files(runs_dir)
    return candidates[-1] if candidates else None


def wait_for_new_jsonl(
    log_dir: Path,
    exclude: set[Path],
    should_continue: Callable[[], bool],
    timeout: float = 12.0,
    poll: float = 0.1,
) -> Optional[Path]:
    """Poll *log_dir* until a JSONL file not in *exclude* appears.

    Returns the new file, or ``None`` if the timeout elapses or
    ``should_continue()`` goes false (the process died before writing a log).
    """
    deadline = time.monotonic() + timeout
    exclude = {p.resolve() for p in exclude}
    while time.monotonic() < deadline:
        if log_dir.is_dir():
            candidates = [
                p
                for p in log_dir.glob(JSONL_GLOB)
                if p.resolve() not in exclude
            ]
            if candidates:
                return sorted(candidates, key=lambda p: p.name)[-1]
        if not should_continue():
            # Process exited; give the file a last chance to have shown up.
            if log_dir.is_dir():
                candidates = [
                    p
                    for p in log_dir.glob(JSONL_GLOB)
                    if p.resolve() not in exclude
                ]
                if candidates:
                    return sorted(candidates, key=lambda p: p.name)[-1]
            return None
        time.sleep(poll)
    return None


def follow_lines(
    path: Path,
    should_stop: Callable[[], bool],
    poll: float = 0.08,
) -> Iterator[str]:
    """Yield complete lines from *path* as it grows (``tail -f`` semantics).

    Partial trailing writes (a line without its newline yet) are buffered until
    the newline arrives. When ``should_stop()`` becomes true the file is drained
    one final time — including any last unterminated line — and the generator
    ends. Robust to the writer flushing mid-line.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        leftover = ""
        while True:
            data = fh.read()
            if data:
                leftover += data
                *complete, leftover = leftover.split("\n")
                for ln in complete:
                    yield ln
                continue
            if should_stop():
                data = fh.read()
                if data:
                    leftover += data
                    *complete, leftover = leftover.split("\n")
                    for ln in complete:
                        yield ln
                if leftover.strip():
                    yield leftover
                return
            time.sleep(poll)


class LiveLogView:
    """Renderable state for the live log viewport.

    Holds a ring buffer of the most recent records plus running per-level
    counters, and renders a header (spinner + file + elapsed + counts), the
    scrolling body, and a footer hint. :meth:`render` is pure — call it whenever
    you want the current frame.
    """

    def __init__(
        self,
        console: Console,
        title: str,
        log_path: Optional[Path] = None,
        buffer: int = 2000,
        min_level: str = DEFAULT_MIN_LEVEL,
    ) -> None:
        self.console = console
        self.title = title
        self.log_path = log_path
        self.records: Deque[LiveRecord] = deque(maxlen=buffer)
        self.counts: dict[str, int] = {lvl: 0 for lvl in _LEVEL_ORDER}
        self.latest_elapsed = 0.0
        self.total = 0
        self.progress = RunProgress()
        # Body hides records below this level (trace/debug by default). Counts and
        # the mission/step breadcrumb still see every record — only the scrolling
        # body is filtered, so the debug-level mission-preload markers still build
        # the breadcrumb.
        self._min_rank = level_rank(min_level)
        self._spinner = Spinner("dots", style="cyan")

    def push(self, rec: LiveRecord) -> None:
        self.total += 1
        self.latest_elapsed = rec.elapsed
        self.progress.update(rec)
        if rec.level in self.counts:
            self.counts[rec.level] += 1
        else:
            self.counts[rec.level] = 1
        if level_rank(rec.level) >= self._min_rank:
            self.records.append(rec)

    @property
    def warn_error_count(self) -> int:
        return (
            self.counts.get("WARN", 0)
            + self.counts.get("ERROR", 0)
            + self.counts.get("CRITICAL", 0)
        )

    def _visible_rows(self) -> int:
        # Leave room for header panel (3), footer (1), and a little breathing space.
        return max(4, self.console.size.height - 6)

    def _counts_text(self) -> Text:
        t = Text()
        first = True
        for lvl in _LEVEL_ORDER:
            n = self.counts.get(lvl, 0)
            if not n and lvl in ("TRACE",):
                continue
            if not first:
                t.append("  ")
            first = False
            t.append(f"{lvl.title()} ", style=_level_style(lvl) or "white")
            t.append(str(n), style="bold " + (_level_style(lvl) or "white"))
        return t

    def _breadcrumb_text(self) -> Text:
        """The resolved ``phase · mission · step`` position, coloured by phase."""
        p = self.progress
        t = Text()
        phase = p.phase
        mission = p.mission_label()
        step = p.step_label()
        if not (phase or mission or step):
            t.append("waiting for mission…", style="dim")
            return t
        if phase:
            t.append(phase, style=_PHASE_STYLES.get(phase, "cyan"))
        if mission:
            if len(t):
                t.append("  ", style="dim")
            t.append(mission, style="bold")
        if step:
            if len(t):
                t.append("  ›  ", style="dim")
            t.append(step, style="cyan")
        return t

    def _header(self) -> Panel:
        head = Table.grid(expand=True, padding=(0, 1))
        head.add_column(justify="left", ratio=1)
        head.add_column(justify="right")

        left = Table.grid(padding=(0, 1))
        left.add_column()
        left.add_column()
        loc = self.log_path.name if self.log_path else "waiting for log…"
        title = Text()
        title.append(self.title, style="bold cyan")
        title.append("  ")
        title.append(loc, style="dim")
        left.add_row(self._spinner, title)

        right = Text()
        right.append(f"{self.latest_elapsed:6.2f}s", style="bold")
        right.append("  ")
        right.append_text(self._counts_text())

        head.add_row(left, right)

        # Second row: the resolved mission/step breadcrumb on its own full-width
        # line (its own grid so the counts column above can't squeeze it into a
        # wrap). Ellipsis-truncated rather than wrapped.
        crumb = Table.grid(expand=True)
        crumb.add_column(justify="left", no_wrap=True, overflow="ellipsis")
        crumb.add_row(self._breadcrumb_text())

        return Panel(Group(head, crumb), border_style="cyan", padding=(0, 1))

    def _body(self) -> RenderableType:
        rows = list(self.records)[-self._visible_rows() :]
        table = Table(
            box=None,
            show_header=False,
            expand=True,
            pad_edge=False,
            padding=(0, 1),
        )
        # Clean three-column layout — just elapsed, level and message. The
        # emitting source/func is dropped from the live view (it's noise for
        # following a run); it's still in the file and in `raccoon logs`.
        table.add_column("elapsed", justify="right", width=9, style="dim", no_wrap=True)
        table.add_column("level", width=5, no_wrap=True)
        table.add_column("message", ratio=1, no_wrap=True, overflow="ellipsis")

        if not rows:
            table.add_row("", "", Text("waiting for first log record…", style="dim"))
            return table

        for rec in rows:
            lvl_style = _level_style(rec.level)
            msg_style = lvl_style if rec.level in _LOUD else ""
            table.add_row(
                f"{rec.elapsed:7.3f}s",
                Text(rec.level[:5], style=lvl_style),
                Text(rec.message, style=msg_style),
            )
        return table

    def _footer(self) -> Text:
        t = Text()
        t.append("Ctrl+C", style="bold")
        t.append(" stop", style="dim")
        t.append("   •   ", style="dim")
        t.append("full log after run: ", style="dim")
        t.append("raccoon logs", style="cyan")
        t.append("   •   ", style="dim")
        t.append("--raw", style="cyan")
        t.append(" for plain stdout", style="dim")
        return t

    def render(self) -> Group:
        return Group(self._header(), self._body(), self._footer())


def wait_for_path(
    path: Path,
    should_continue: Callable[[], bool],
    timeout: float = 12.0,
    poll: float = 0.1,
) -> bool:
    """Poll until *path* exists, the process dies, or *timeout* elapses.

    Returns ``True`` once the file exists (giving the writer one last check after
    the process exits), ``False`` otherwise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        if not should_continue():
            return path.is_file()
        time.sleep(poll)
    return path.is_file()


def stream_run_logs(
    log_dir: Path,
    is_running: Callable[[], bool],
    console: Console,
    title: str,
    existing: Optional[set[Path]] = None,
    startup_timeout: float = 12.0,
    log_path: Optional[Path] = None,
) -> bool:
    """Stream the current run's JSONL log into a live TUI.

    When *log_path* is given (``raccoon run`` knows the exact
    ``<run_dir>/libstp.jsonl`` the child will write), the streamer waits for that
    file specifically — race-free. Otherwise it waits for a fresh
    ``libstp-*.jsonl`` (one not already in *existing*) to appear. Either way it
    then tails the file into a :class:`LiveLogView` until ``is_running()`` goes
    false and the file is fully drained.

    Returns ``True`` if a log file was found and streamed, ``False`` if none
    appeared before the process exited / the timeout elapsed (so the caller can
    fall back to a plain message).
    """
    from rich.live import Live

    existing = existing or set()
    if log_path is not None:
        if not wait_for_path(log_path, should_continue=is_running, timeout=startup_timeout):
            return False
    else:
        log_path = wait_for_new_jsonl(
            log_dir, exclude=existing, should_continue=is_running, timeout=startup_timeout
        )
        if log_path is None:
            return False

    view = LiveLogView(console, title=title, log_path=log_path)
    with Live(
        view.render(),
        console=console,
        auto_refresh=True,
        refresh_per_second=12,
        screen=False,
        transient=False,
    ) as live:
        for line in follow_lines(log_path, should_stop=lambda: not is_running()):
            rec = parse_record(line)
            if rec is None:
                continue
            view.push(rec)
            live.update(view.render())
        # Final frame so the last records and counts are visible after exit.
        live.update(view.render())
    return True
