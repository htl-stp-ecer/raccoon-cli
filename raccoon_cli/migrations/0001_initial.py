"""Migration 0001: Baseline marker. No changes required."""

from pathlib import Path


NUMBER = 1
DESCRIPTION = "Baseline (original project structure)"


def run(project_root: Path) -> None:
    """No-op baseline migration."""
    pass
