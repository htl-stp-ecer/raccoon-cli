"""Locate log directories and files."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Optional

from .parser import LogRun, detect_runs, parse_log_file, single_run
from .run_cache import load_cached_run, write_cached_run


# New scheme: one file per run, named ``libstp-<timestamp>.jsonl`` (e.g.
# ``libstp-2026-07-01_14-30-00.jsonl``). The zero-padded timestamp sorts
# lexicographically, so sorting file names by string == chronological order.
# Pre-JSONL builds wrote the same per-run name with a ``.log`` extension (still
# a single run per file, pipe-delimited text); those are read too.
_RUN_GLOBS = ("libstp-*.jsonl", "libstp-*.log")

# Legacy spdlog rotation scheme (single growing ``libstp.log`` + numbered
# rotations). Kept so ``raccoon logs`` still reads log dirs written by older
# library builds. Ordered oldest → newest.
_LEGACY_ROTATED_NAMES = ["libstp.3.log", "libstp.2.log", "libstp.1.log", "libstp.log"]


def _run_sort_key(path: Path) -> tuple[str, int]:
    """Sort per-run files chronologically; prefer ``.jsonl`` on a timestamp tie.

    The stem (``libstp-<timestamp>``) sorts chronologically. When a run has both
    a ``.jsonl`` and a ``.log`` file for the same timestamp, the ``.jsonl`` gets
    the higher secondary rank so it lands last (== newest / current run).
    """
    return (path.stem, 1 if path.suffix == ".jsonl" else 0)

# Default cap for the run list: parse at most this many of the newest files so
# `raccoon logs` stays fast when a project has accumulated hundreds of runs.
# Older runs remain accessible by explicit index or with a larger ``-n``.
DEFAULT_LIST_LIMIT = 25


def find_log_dir(start: Optional[Path] = None) -> Optional[Path]:
    """Find the ``.raccoon/logs/`` directory by searching upward from *start*.

    Checks:
    1. ``{start}/.raccoon/logs/`` (CWD or project root)
    2. Walk up to 5 parents looking for a ``.raccoon/logs/`` with log files
    """
    if start is None:
        start = Path.cwd()
    start = start.resolve()

    candidate = start / ".raccoon" / "logs"
    if _is_log_dir(candidate):
        return candidate

    current = start
    for _ in range(5):
        parent = current.parent
        if parent == current:
            break
        candidate = parent / ".raccoon" / "logs"
        if _is_log_dir(candidate):
            return candidate
        current = parent

    return None


def _is_log_dir(path: Path) -> bool:
    """Check if a directory looks like a libstp log directory."""
    if not path.is_dir():
        return False
    return bool(discover_log_files(path))


def discover_log_files(log_dir: Path, include_legacy: bool = True) -> List[Path]:
    """Return log files in chronological order (oldest first).

    Prefers the new per-run scheme (``libstp-<timestamp>.jsonl``, plus the older
    ``.log`` per-run text files). When *include_legacy* is set, older log dirs'
    rotation files (``libstp.log`` and ``libstp.N.log``) are prepended as the
    oldest history.
    """
    files: List[Path] = []

    # Legacy rotation files, if present, are older history — put them first.
    if include_legacy:
        for name in _LEGACY_ROTATED_NAMES:
            p = log_dir / name
            if p.exists():
                files.append(p)

    # New per-run files (.jsonl + legacy per-run .log), sorted so the timestamp
    # orders chronologically and .jsonl wins a same-timestamp tie (oldest first).
    per_run: List[Path] = []
    for glob in _RUN_GLOBS:
        per_run.extend(log_dir.glob(glob))
    files.extend(sorted(per_run, key=_run_sort_key))
    return files


def current_log_file(log_dir: Path) -> Optional[Path]:
    """Return the newest (current run's) log file, or None if there are none."""
    files = discover_log_files(log_dir)
    return files[-1] if files else None


def is_run_file(path: Path) -> bool:
    """True if *path* is a per-run file (``libstp-<timestamp>.jsonl`` or ``.log``).

    Per-run files hold exactly one run; legacy ``libstp.log`` may hold several.
    """
    return any(fnmatch.fnmatch(path.name, glob) for glob in _RUN_GLOBS)


def load_runs(files: List[Path], limit: Optional[int] = None) -> List[LogRun]:
    """Load runs from *files* (oldest → newest), treating each file separately.

    Each per-run file (``libstp-<timestamp>.log``) is exactly one run — no
    boundary heuristic. Legacy single files may contain several runs, so those
    are still split with ``detect_runs``. Runs are never merged across files and
    are re-indexed globally (most recent = 1).

    Parsing every line of every file is the slow part, so *limit* caps how many
    of the **newest** files are actually parsed (the rest are skipped entirely).
    Because indices are always counted from the newest run, the returned indices
    are identical to an unlimited load — you just get fewer, older runs. Use
    :func:`load_run_by_index` to fetch a single run without parsing the rest.

    Per-run files additionally use a summary sidecar cache (see
    :mod:`raccoon_cli.logs.run_cache`): a completed file is parsed once, then
    subsequent listings read its cached summary instead of re-parsing. Only the
    summary (times, counts, sources) is cached — full entries are never loaded
    on the list path, which no caller needs there.
    """
    parse_files = files
    if limit is not None and limit > 0 and len(files) > limit:
        parse_files = files[-limit:]

    runs: List[LogRun] = []
    for f in parse_files:
        if is_run_file(f):
            cached = load_cached_run(f)
            if cached is not None:
                runs.append(cached)
                continue
            entries = parse_log_file(f)
            if not entries:
                continue
            run = single_run(entries)
            if run is not None:
                write_cached_run(f, run)
                runs.append(run)
        else:
            # Legacy multi-run files aren't cached — they can hold several runs
            # and are a rare fallback path.
            entries = parse_log_file(f)
            if not entries:
                continue
            runs.extend(detect_runs(entries))

    # Re-index across parsed files: newest run = 1 (files came oldest-first).
    for i, run in enumerate(reversed(runs)):
        run.index = i + 1
    return runs


def load_run_by_index(files: List[Path], index: int) -> Optional[LogRun]:
    """Load a single run by its (newest = 1) index, parsing as little as possible.

    When every file is a per-run file (the common case), run *index* maps
    directly to a single file from the newest end, so only that one file is
    parsed. If any legacy multi-run file is present the mapping no longer holds,
    so this falls back to a full :func:`load_runs`.
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
        return run

    # Legacy files break the 1-file-per-run mapping; parse everything.
    runs = load_runs(files)
    return next((r for r in runs if r.index == index), None)
