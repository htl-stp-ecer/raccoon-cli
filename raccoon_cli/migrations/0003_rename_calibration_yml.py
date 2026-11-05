"""Migration 0003: Rename racoon.calibration.yml (typo) to raccoon.calibration.yml."""

from __future__ import annotations

from pathlib import Path

NUMBER = 3
DESCRIPTION = "Rename racoon.calibration.yml to raccoon.calibration.yml"

OLD_NAME = "racoon.calibration.yml"
NEW_NAME = "raccoon.calibration.yml"


def run(project_root: Path) -> None:
    """Rename the calibration file and fix the .gitignore entry."""
    old = project_root / OLD_NAME
    new = project_root / NEW_NAME

    if old.exists():
        if new.exists():
            # Both exist: keep the new file, don't overwrite it.
            print(f"  {NEW_NAME} already exists, leaving {OLD_NAME} untouched")
        else:
            old.rename(new)

    _fix_gitignore(project_root / ".gitignore")


def _fix_gitignore(gitignore: Path) -> None:
    if not gitignore.exists():
        return
    content = gitignore.read_text()
    if OLD_NAME not in content:
        return
    gitignore.write_text(content.replace(OLD_NAME, NEW_NAME))
