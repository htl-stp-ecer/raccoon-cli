"""Parse libstp log files and detect run boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


# Log line format:
# 2026-04-12 18:15:04 |     3.444s | info     | p.Motor.cpp                    | Mock Motor ...
_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"  # timestamp
    r" \|\s+"
    r"(\d+\.\d+)s"                                # elapsed seconds
    r" \|\s+"
    r"(\w+)"                                       # level
    r"\s+\|\s+"
    r"(.*?)"                                       # source (may be empty)
    r"\s*\|\s+"                                    # delimiter
    r"(.*)$"                                       # message
)

# The "Logging to directory" message marks the start of a new run.
_RUN_START_RE = re.compile(r"^Logging to directory:")


@dataclass
class LogEntry:
    """A single parsed log line."""

    timestamp: datetime
    elapsed: float
    level: str
    source: str
    message: str
    line_number: int = 0
    file_path: str = ""

    @property
    def level_upper(self) -> str:
        lvl = self.level.upper()
        # Normalize "WARNING" → "WARN" for consistency
        if lvl == "WARNING":
            return "WARN"
        return lvl


@dataclass
class LogRun:
    """A group of log entries belonging to one program execution."""

    index: int  # 1-based, most recent = 1
    start_time: datetime
    end_time: datetime
    duration_secs: float
    entries: List[LogEntry] = field(default_factory=list)
    file_path: str = ""

    @property
    def line_count(self) -> int:
        return len(self.entries)

    @property
    def level_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            key = e.level_upper
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def sources(self) -> set[str]:
        return {e.source for e in self.entries if e.source.strip()}


def parse_log_line(line: str, line_number: int = 0, file_path: str = "") -> Optional[LogEntry]:
    """Parse a single log line. Returns None if the line doesn't match."""
    m = _LOG_RE.match(line.strip())
    if not m:
        return None
    ts_str, elapsed_str, level, source, message = m.groups()
    return LogEntry(
        timestamp=datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S"),
        elapsed=float(elapsed_str),
        level=level.strip(),
        source=source.strip(),
        message=message.strip(),
        line_number=line_number,
        file_path=file_path,
    )


def parse_log_file(path: Path) -> List[LogEntry]:
    """Parse all valid log entries from a file."""
    entries: List[LogEntry] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, 1):
            # Handle concatenated lines (rotation boundary where newline is missing)
            # Split on date pattern that appears mid-line
            segments = re.split(r"(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \|)", raw)
            for seg in segments:
                entry = parse_log_line(seg, line_number=line_no, file_path=str(path))
                if entry:
                    entries.append(entry)
    return entries


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
