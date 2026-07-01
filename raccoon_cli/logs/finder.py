"""Locate log directories and files."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional


# New scheme: one file per run, named ``libstp-<timestamp>.log`` (e.g.
# ``libstp-2026-07-01_14-30-00.log``). The zero-padded timestamp sorts
# lexicographically, so sorting file names by string == chronological order.
_RUN_GLOB = "libstp-*.log"

# Legacy spdlog rotation scheme (single growing ``libstp.log`` + numbered
# rotations). Kept so ``raccoon logs`` still reads log dirs written by older
# library builds. Ordered oldest → newest.
_LEGACY_ROTATED_NAMES = ["libstp.3.log", "libstp.2.log", "libstp.1.log", "libstp.log"]


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


def discover_log_files(log_dir: Path) -> List[Path]:
    """Return log files in chronological order (oldest first).

    Prefers the new per-run scheme (``libstp-<timestamp>.log``); falls back to
    the legacy rotation names for log dirs written by older library builds.
    """
    files: List[Path] = []

    # Legacy rotation files, if present, are older history — put them first.
    for name in _LEGACY_ROTATED_NAMES:
        p = log_dir / name
        if p.exists():
            files.append(p)

    # New per-run files, sorted by name (== chronological thanks to the
    # zero-padded timestamp), oldest first.
    files.extend(sorted(log_dir.glob(_RUN_GLOB), key=lambda p: p.name))
    return files


def current_log_file(log_dir: Path) -> Optional[Path]:
    """Return the newest (current run's) log file, or None if there are none."""
    files = discover_log_files(log_dir)
    return files[-1] if files else None
