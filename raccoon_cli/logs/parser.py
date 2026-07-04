"""Parse libstp log files and detect run boundaries.

The library (raccoon-lib) writes one **JSONL** file per run at
``.raccoon/runs/<run_id>/libstp.jsonl`` — one JSON object per line carrying all
metadata (``t``, ``elapsed``, ``seq``, ``level``, ``logger``, ``thread``,
``pid``, ``file``, ``line``, ``func``, ``msg``). JSONL is the only supported
format; :func:`parse_log_file` parses every file as JSONL.

The JSONL field handling here is the single source of truth: the live
``raccoon run`` TUI (:mod:`raccoon_cli.logs.live_stream`) builds its
``LiveRecord`` from :func:`parse_jsonl_line` too, so the live streamer and the
post-hoc ``raccoon logs`` viewer never diverge on how a record is decoded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Fallback timestamp for JSONL records with a missing/unparseable ``t`` — real
# files always carry one, but a truncated/garbled line must not crash the parse.
_EPOCH = datetime(1970, 1, 1)


# The "Logging to directory" message marks the start of a new run.
_RUN_START_RE = re.compile(r"^Logging to directory:")


def humanize_source(source: str) -> str:
    """Shorten a log ``source`` to its last two components (parent + file).

    The library's log formatter abbreviates every parent directory of a source
    file to its first character. For C++ files (built from the repo) that yields
    a tidy ``m.file.cpp``. For the *installed* Python package, though, it
    abbreviates the whole absolute path, so a source shows up as noise like
    ``h.t...l.p.s.r.r.api.py`` (home / .venv / lib / python / site-packages /
    raccoon / robot / api.py).

    We can't recover the abbreviated names, but the last two dotted components
    (``<parent-initial>.<filename>``) are the useful, disambiguating part — and
    already-tidy C++ sources (``d.drive.cpp``) are left unchanged. Keeping just
    those drops the venv/site-packages noise uniformly.
    """
    if not source:
        return source
    segments = source.split(".")
    # ``name.ext`` is the last two segments; anything before is directory
    # abbreviations. Three or fewer segments is already ``parent.name.ext`` (or
    # shorter), so leave it alone.
    if len(segments) <= 3:
        return source
    parent = segments[-3]
    filename = f"{segments[-2]}.{segments[-1]}"
    return f"{parent}.{filename}"


@dataclass
class LogEntry:
    """A single parsed log line (from either the JSONL or legacy text format).

    ``source`` is the short, groupable emitter label: the file **basename** for
    JSONL records (e.g. ``base.py``) or the library's dotted abbreviation for
    legacy text (e.g. ``p.Motor.cpp``). ``source_path`` keeps the full source
    path and ``line_number`` the source line (JSONL only; ``line_number`` is 0
    for legacy text, which carries no reliable source line), so a richer
    ``file:line`` location and the enclosing ``func`` can be rendered without
    losing the groupable source. ``file_path`` is the *log file* this entry was
    read from (used to locate the raw file for ``download``), never the source
    file. ``seq``/``thread``/``pid`` are carried through from JSONL (0 for legacy).
    """

    timestamp: datetime
    elapsed: float
    level: str
    source: str
    message: str
    line_number: int = 0
    file_path: str = ""
    func: str = ""
    seq: int = 0
    thread: int = 0
    pid: int = 0
    source_path: str = ""

    @property
    def level_upper(self) -> str:
        lvl = self.level.upper()
        # Normalize "WARNING" (spdlog's spelling) → "WARN" for consistency
        if lvl == "WARNING":
            return "WARN"
        return lvl

    @property
    def location(self) -> str:
        """``source:line`` (or just ``source``) — the emitting location."""
        if self.source and self.line_number:
            return f"{self.source}:{self.line_number}"
        return self.source


@dataclass
class LogRun:
    """A group of log entries belonging to one program execution."""

    index: int  # 1-based, most recent = 1
    start_time: datetime
    end_time: datetime
    duration_secs: float
    entries: List[LogEntry] = field(default_factory=list)
    file_path: str = ""

    # Unified per-run artifact directory (``.raccoon/runs/<run_id>/``). ``run_id``
    # is the ``YYYYMMDDThhmmssZ`` directory name. Both are set by the finder after
    # the run is built; the log this run parses from is ``file_path``
    # (``<run_dir>/libstp.jsonl``). Downloads use ``run_dir`` to bundle
    # localization/profile too.
    run_dir: Optional[str] = None
    run_id: Optional[str] = None

    # When a run is loaded from the metadata sidecar cache its raw ``entries``
    # aren't parsed (that's the whole point — parsing a multi-MB file is the
    # bottleneck). These hold the precomputed summary so the list view is
    # correct without the entries. ``None`` means "not cached, derive from
    # entries". See ``logs.run_cache``.
    summary_line_count: Optional[int] = None
    summary_level_counts: Optional[dict[str, int]] = None
    summary_sources: Optional[set[str]] = None

    @property
    def line_count(self) -> int:
        if self.summary_line_count is not None:
            return self.summary_line_count
        return len(self.entries)

    @property
    def level_counts(self) -> dict[str, int]:
        if self.summary_level_counts is not None:
            return dict(self.summary_level_counts)
        counts: dict[str, int] = {}
        for e in self.entries:
            key = e.level_upper
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def sources(self) -> set[str]:
        if self.summary_sources is not None:
            return set(self.summary_sources)
        return {e.source for e in self.entries if e.source.strip()}


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _basename(path: str) -> str:
    """Last path component of *path* (handles both ``/`` and ``\\`` separators)."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def parse_jsonl_line(line: str, line_number: int = 0) -> Optional[LogEntry]:
    """Parse one JSONL log line into a :class:`LogEntry`.

    Returns ``None`` for blank lines or anything that isn't a JSON object, so a
    stray non-JSON line (e.g. an interpreter banner that slipped into the file)
    is skipped rather than crashing the parse. This is the shared decoder used
    by both this module and :mod:`raccoon_cli.logs.live_stream`.
    """
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None

    ts_raw = str(rec.get("t", "") or "")
    try:
        timestamp = datetime.fromisoformat(ts_raw) if ts_raw else _EPOCH
    except ValueError:
        timestamp = _EPOCH

    file_field = str(rec.get("file", "") or "")
    return LogEntry(
        timestamp=timestamp,
        elapsed=_coerce_float(rec.get("elapsed", 0.0)),
        level=str(rec.get("level", "") or ""),
        # Basename is the groupable emitter; the full source path is source_path.
        source=_basename(file_field),
        message=str(rec.get("msg", "") or ""),
        line_number=_coerce_int(rec.get("line", 0)),
        func=str(rec.get("func", "") or ""),
        seq=_coerce_int(rec.get("seq", 0)),
        thread=_coerce_int(rec.get("thread", 0)),
        pid=_coerce_int(rec.get("pid", 0)),
        source_path=file_field,
    )


def _parse_jsonl_file(path: Path) -> List[LogEntry]:
    """Parse every JSON object line of a per-run ``.jsonl`` file."""
    entries: List[LogEntry] = []
    log_path = str(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, 1):
            entry = parse_jsonl_line(raw, line_number=line_no)
            if entry is not None:
                # file_path is the *log file* (for download), not the source.
                entry.file_path = log_path
                entries.append(entry)
    return entries


def parse_log_file(path: Path) -> List[LogEntry]:
    """Parse all valid log entries from a run's JSONL log file.

    Every run log is JSONL now (one JSON object per line); non-object lines are
    skipped by :func:`parse_jsonl_line`.
    """
    return _parse_jsonl_file(path)


def detect_runs(entries: List[LogEntry]) -> List[LogRun]:
    """Split a flat list of entries into runs.

    A new run starts when:
    - The elapsed time resets to 0 (or near 0 after a higher value)
    - The message matches "Logging to directory: ..."
    """
    if not entries:
        return []

    runs: List[LogRun] = []
    current_entries: List[LogEntry] = []
    prev_elapsed = -1.0

    for entry in entries:
        is_new_run = False

        if _RUN_START_RE.match(entry.message):
            # Explicit run start marker
            is_new_run = True
        elif entry.elapsed < prev_elapsed - 1.0 and entry.elapsed < 1.0:
            # Elapsed time reset (with some tolerance for out-of-order)
            is_new_run = True

        if is_new_run and current_entries:
            runs.append(_make_run(current_entries, index=0))
            current_entries = []

        current_entries.append(entry)
        prev_elapsed = entry.elapsed

    if current_entries:
        runs.append(_make_run(current_entries, index=0))

    # Assign indices: most recent run = 1
    for i, run in enumerate(reversed(runs)):
        run.index = i + 1

    return runs


def single_run(entries: List[LogEntry], index: int = 0) -> Optional[LogRun]:
    """Build exactly one run from all entries (for per-run log files).

    The new logging scheme writes one file per program run, so the whole file is
    a single run — no boundary detection needed. Returns None for empty input.
    """
    if not entries:
        return None
    return _make_run(entries, index=index)


def _make_run(entries: List[LogEntry], index: int) -> LogRun:
    """Create a LogRun from a list of entries."""
    start = entries[0].timestamp
    end = entries[-1].timestamp
    # Use the max elapsed time as duration (more accurate than wall-clock diff)
    max_elapsed = max(e.elapsed for e in entries)
    return LogRun(
        index=index,
        start_time=start,
        end_time=end,
        duration_secs=max_elapsed,
        entries=list(entries),
        file_path=entries[0].file_path,
    )
