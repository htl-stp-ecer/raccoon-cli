"""Turn a crashed run's stderr (a Python traceback) into JSONL log records.

The library (raccoon-lib) writes one JSONL record per line to
``<run_dir>/libstp.jsonl`` and that file — not the child's stdout/stderr — is the
*only* thing streamed to the live TUI and persisted as the run log. A Python
runtime error, though, is printed to **stderr** as a plain-text traceback, which
never enters that JSONL stream: it is silently dropped by the JSONL viewer (which
skips non-JSON lines) and never reaches the saved log. The run just "quits".

This module closes that gap. :func:`build_crash_records` converts captured
stderr into proper ``ERROR`` JSONL records — one per stderr line, so the
traceback renders cleanly in both the live view and ``raccoon logs`` — and
:func:`append_crash_records` appends them to the run's ``libstp.jsonl`` so the
crash is persisted in the log file. Callers that stream (the remote path) also
write the returned lines to stdout so the laptop's viewer parses and renders
them instead of discarding raw traceback text.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

# A traceback is normally short, but a runaway (deep recursion, a flood of
# warnings) could be enormous. Keep the tail — the innermost frames and the
# exception line, which is what actually diagnoses the crash — and note the drop.
DEFAULT_MAX_LINES = 200


def build_crash_records(
    stderr_text: str,
    *,
    elapsed: float = 0.0,
    pid: int = 0,
    seq_start: int = 1_000_000,
    max_lines: int = DEFAULT_MAX_LINES,
) -> List[str]:
    """Convert captured *stderr* into a list of ``ERROR`` JSONL log lines.

    One record per non-empty stderr line so a multi-line traceback renders as a
    series of readable rows (the JSONL viewers ellipsis-truncate a single record
    to one line). Fields match what :func:`raccoon_cli.logs.parser.parse_jsonl_line`
    reads (``t``, ``elapsed``, ``seq``, ``level``, ``msg``, ``file``…), so the
    records are indistinguishable from ones the library itself emitted.

    *seq_start* is a high base so these records sort after the run's real records.
    Returns ``[]`` for blank input.
    """
    lines = [ln.rstrip("\r") for ln in (stderr_text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return []

    dropped = 0
    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[-max_lines:]
        lines.insert(0, f"… {dropped} earlier stderr line(s) omitted …")

    ts = datetime.now(timezone.utc).isoformat()
    records: List[str] = []
    for i, msg in enumerate(lines):
        rec = {
            "t": ts,
            "elapsed": round(float(elapsed), 3),
            "seq": seq_start + i,
            "level": "ERROR",
            "logger": "runtime",
            "thread": 0,
            "pid": int(pid),
            # A synthetic source so `file:line` reads sensibly and it's clear the
            # record came from the process's stderr, not the library logger.
            "file": "<stderr>",
            "line": 0,
            "func": "",
            "msg": msg,
        }
        records.append(json.dumps(rec, ensure_ascii=False))
    return records


def append_crash_records(log_path: Path, lines: List[str]) -> None:
    """Append pre-built JSONL *lines* to the run log at *log_path*.

    Creates the file (and parent) if the child crashed before the library wrote
    anything, and inserts a leading newline if the existing file ended mid-line
    (a partial flush), so the appended records are never glued onto a real one.
    Best-effort: any I/O error is swallowed — persisting the crash must never mask
    the original crash.
    """
    if not lines:
        return
    try:
        log_path = Path(log_path)
        needs_newline = False
        if log_path.exists() and log_path.stat().st_size > 0:
            with open(log_path, "rb") as fh:
                fh.seek(-1, 2)
                needs_newline = fh.read(1) != b"\n"
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            if needs_newline:
                fh.write("\n")
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
