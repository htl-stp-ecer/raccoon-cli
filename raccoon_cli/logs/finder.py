"""Locate log directories and files."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional


# Log files ordered from oldest to newest (spdlog rotation names)
_ROTATED_NAMES = ["libstp.3.log", "libstp.2.log", "libstp.1.log", "libstp.log"]


def find_log_dir(start: Optional[Path] = None) -> Optional[Path]:
    """Find the ``.raccoon/logs/`` directory by searching upward from *start*.

    Checks:
    1. ``{start}/.raccoon/logs/`` (CWD or project root)
    2. Walk up to 5 parents looking for a ``.raccoon/logs/libstp.log``
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
    return (path / "libstp.log").exists()


def discover_log_files(log_dir: Path) -> List[Path]:
    """Return log files in chronological order (oldest first)."""
    files: List[Path] = []
    for name in _ROTATED_NAMES:
        p = log_dir / name
        if p.exists():
            files.append(p)
    return files
