"""Tests for local git history helpers."""

import shutil
import subprocess
from pathlib import Path

import pytest

from raccoon_cli.git_history import create_pre_sync_snapshot, initialize_project_history

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


def test_pre_sync_snapshot_skips_when_repo_has_no_changes(tmp_path: Path):
    (tmp_path / "raccoon.project.yml").write_text("name: Demo\n", encoding="utf-8")
    initialize_project_history(tmp_path, "DemoBot")

    result = create_pre_sync_snapshot(
        project_root=tmp_path,
        direction="push",
        target="pi@192.168.4.1:/home/pi/programs/demo",
    )

    assert result.created is False
    assert result.reason == "no_changes"


def test_pre_sync_snapshot_creates_commit_with_structured_message(tmp_path: Path):
    config_path = tmp_path / "raccoon.project.yml"
    config_path.write_text("name: Demo\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    main_path = src / "main.py"
    main_path.write_text("print('v1')\n", encoding="utf-8")
    initialize_project_history(tmp_path, "DemoBot")

    main_path.write_text("print('v2')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    config_path.unlink()

    result = create_pre_sync_snapshot(
        project_root=tmp_path,
        direction="push",
        target="pi@192.168.4.1:/home/pi/programs/demo",
    )

    assert result.created is True
    assert result.commit_sha is not None
    assert result.summary == "3 files (+1 ~1 -1)"

    body = _git(tmp_path, "log", "-1", "--pretty=%B").stdout
    assert "Direction: push" in body
    assert "Target: pi@192.168.4.1:/home/pi/programs/demo" in body
    assert "Summary: 3 files (+1 ~1 -1)" in body
    assert "M src/main.py" in body
    assert "A README.md" in body
    assert "D raccoon.project.yml" in body


def test_pre_sync_snapshot_skips_when_not_git_repo(tmp_path: Path):
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")

    result = create_pre_sync_snapshot(
        project_root=tmp_path,
        direction="push",
        target="pi@192.168.4.1:/home/pi/programs/demo",
    )

    assert result.created is False
    assert result.reason == "not_git_repo"
