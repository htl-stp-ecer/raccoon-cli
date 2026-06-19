"""Tests for local git history helpers.

Pre-sync safety snapshots are no longer real commits; they live in
:mod:`raccoon_cli.checkpoint` as invisible refs and are covered by
``tests/test_checkpoint.py``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from raccoon_cli.git_history import initialize_project_history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def test_initialize_project_history_creates_repo_and_initial_commit(tmp_path: Path):
    (tmp_path / "raccoon.project.yml").write_text("name: Demo\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = initialize_project_history(tmp_path, "DemoBot")

    assert result.initialized is True
    assert result.commit_created is True
    assert result.commit_sha is not None
    assert (tmp_path / ".git").exists()

    subject = _git(tmp_path, "log", "-1", "--pretty=%s").stdout.strip()
    assert subject == "chore(project): scaffold DemoBot"
