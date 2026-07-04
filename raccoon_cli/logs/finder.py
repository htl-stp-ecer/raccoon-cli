"""Locate run directories and their log files under ``.raccoon/runs/``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from .parser import LogRun, detect_runs, parse_log_file, single_run
from .run_cache import load_cached_run, write_cached_run


# Unified per-run artifact directory scheme: ``.raccoon/runs/<run_id>/`` where
# ``run_id`` is a compact UTC timestamp (``YYYYMMDDThhmmssZ``, same form as
# ``ide.repositories.run_repository``). The C++ logger ALWAYS writes
# ``libstp.jsonl`` there — into ``$LIBSTP_LOG_DIR`` when ``raccoon run`` sets it,
# or a self-allocated ``.raccoon/runs/<run_id>/`` for a standalone run.
# Localization/profile artifacts sit next to the log in the same dir.
_RUNS_DIRNAME = "runs"
_RUN_DIR_LOG_NAME = "libstp.jsonl"
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def is_run_dir_log(path: Path) -> bool:
    """True if *path* is a run-dir log (``.raccoon/runs/<run_id>/libstp.jsonl``)."""
    return (
        path.name == _RUN_DIR_LOG_NAME
        and _RUN_ID_RE.match(path.parent.name) is not None
    )


def run_dir_of(path: Path) -> Optional[Path]:
    """Return the run directory for a run-dir log, else ``None``."""
    return path.parent if is_run_dir_log(path) else None


def run_id_of(path: Path) -> Optional[str]:
    """Return the ``run_id`` (dir name) for a run-dir log, else ``None``."""
    return path.parent.name if is_run_dir_log(path) else None


def _annotate_run_dir(run: LogRun) -> LogRun:
    """Attach ``run_dir``/``run_id`` from the run-dir log this run was parsed from."""
    if run.file_path:
        p = Path(run.file_path)
        if is_run_dir_log(p):
            run.run_dir = str(p.parent)
            run.run_id = p.parent.name
    return run


# Default cap for the run list: parse at most this many of the newest runs so
# `raccoon logs` stays fast when a project has accumulated hundreds of runs.
# Older runs remain accessible by explicit index or with a larger ``-n``.
DEFAULT_LIST_LIMIT = 25


def find_log_dir(start: Optional[Path] = None) -> Optional[Path]:
    """Find the ``.raccoon/runs/`` directory by searching upward from *start*.

    Checks:
    1. ``{start}/.raccoon/runs/`` (CWD or project root)
    2. Walk up to 5 parents looking for a ``.raccoon/runs/`` that holds run dirs
    """
    if start is None:
        start = Path.cwd()
    start = start.resolve()

    candidate = start / ".raccoon" / _RUNS_DIRNAME
    if _is_log_dir(candidate):
        return candidate

    current = start
    for _ in range(5):
        parent = current.parent
        if parent == current:
            break
        candidate = parent / ".raccoon" / _RUNS_DIRNAME
        if _is_log_dir(candidate):
            return candidate
        current = parent

    return None


def _is_log_dir(path: Path) -> bool:
    """True if *path* is a ``.raccoon/runs/`` dir holding at least one run's log."""
    return bool(discover_log_files(path))


def discover_log_files(runs_dir: Path) -> List[Path]:
    """Return run logs (``<run_id>/libstp.jsonl``) in chronological order.

    *runs_dir* is the ``.raccoon/runs/`` directory. Each subdirectory named with a
    valid ``run_id`` (``YYYYMMDDThhmmssZ``) that holds a ``libstp.jsonl`` is one
    run; the zero-padded UTC ``run_id`` sorts oldest → newest. Directories with an
    invalid name (or no log yet) are skipped silently — they may be in-progress
    writes or junk. *runs_dir* need not exist (returns an empty list).
    """
    if not runs_dir.is_dir():
        return []
    found: List[Path] = []
    for entry in runs_dir.iterdir():
        if not entry.is_dir() or not _RUN_ID_RE.match(entry.name):
            continue
        log_file = entry / _RUN_DIR_LOG_NAME
        if log_file.is_file():
            found.append(log_file)
    return sorted(found, key=lambda p: p.parent.name)


def current_log_file(runs_dir: Path) -> Optional[Path]:
    """Return the newest (current run's) log file, or None if there are none."""
    files = discover_log_files(runs_dir)
    return files[-1] if files else None


def is_run_file(path: Path) -> bool:
    """True if *path* holds exactly one run — a ``<run_id>/libstp.jsonl`` log."""
    return is_run_dir_log(path)


def load_runs(files: List[Path], limit: Optional[int] = None) -> List[LogRun]:
    """Load runs from *files* (oldest → newest), one run per file.

    Each file is a single run's ``libstp.jsonl`` — no boundary heuristic. Runs are
    re-indexed globally (most recent = 1).

    Parsing every line of every file is the slow part, so *limit* caps how many of
    the **newest** files are actually parsed (the rest are skipped entirely).
    Because indices are always counted from the newest run, the returned indices
    are identical to an unlimited load — you just get fewer, older runs. Use
    :func:`load_run_by_index` to fetch a single run without parsing the rest.

    Each run additionally uses a summary sidecar cache (see
    :mod:`raccoon_cli.logs.run_cache`): a completed file is parsed once, then
    subsequent listings read its cached summary instead of re-parsing. Only the
    summary (times, counts, sources) is cached — full entries are never loaded on
    the list path, which no caller needs there.
    """
    parse_files = files
    if limit is not None and limit > 0 and len(files) > limit:
        parse_files = files[-limit:]

    runs: List[LogRun] = []
    for f in parse_files:
        if is_run_file(f):
            cached = load_cached_run(f)
            if cached is not None:
                runs.append(_annotate_run_dir(cached))
                continue
            entries = parse_log_file(f)
            if not entries:
                continue
            run = single_run(entries)
            if run is not None:
                write_cached_run(f, run)
                runs.append(_annotate_run_dir(run))
        else:
            # Not a single-run file — parse and split defensively. In practice
            # discovery only yields run-dir logs, so this branch is unused.
            entries = parse_log_file(f)
            if not entries:
                continue
            runs.extend(detect_runs(entries))

    # Re-index across parsed files: newest run = 1 (files came oldest-first).
    for i, run in enumerate(reversed(runs)):
        run.index = i + 1
    return runs


def load_run_by_index(files: List[Path], index: int) -> Optional[LogRun]:
    """Load a single run by its (newest = 1) index, parsing only that run's file.

    Every discovered file is a single-run ``libstp.jsonl``, so run *index* maps
    directly to one file from the newest end.
    """
    if index < 1 or not files:
        return None

    if all(is_run_file(f) for f in files):
        if index > len(files):
            return None
        target = files[-index]  # newest = 1 → last file
        run = single_run(parse_log_file(target))
        if run is not None:
            run.index = index
            _annotate_run_dir(run)
        return run

    # Mixed input breaks the 1-file-per-run mapping; parse everything.
    runs = load_runs(files)
    return next((r for r in runs if r.index == index), None)
