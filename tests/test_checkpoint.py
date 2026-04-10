"""Tests for invisible git checkpoints."""

import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from raccoon_cli.checkpoint import (
    _MAX_CHECKPOINTS,
    _prune_excess_checkpoints,
    clean_checkpoints,
    create_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    restore_checkpoint,
    show_checkpoint_diff,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path) -> None:
    """Create a git repo with an initial commit."""
    _git(tmp_path, "init", "--initial-branch", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@test")
    (tmp_path / "file.txt").write_text("initial\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "initial")


class TestCreateCheckpoint:
    def test_creates_ref_without_affecting_branch_or_stash(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("modified\n")

        head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        stash_before = _git(tmp_path, "stash", "list").stdout.strip()

        result = create_checkpoint(tmp_path, label="test")

        assert result.created is True
        assert result.sha is not None
        assert result.short_sha is not None
        assert result.ref is not None
        assert result.ref.startswith("refs/raccoon/checkpoints/")
        assert "test" in result.ref

        # Branch HEAD unchanged
        head_after = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert head_after == head_before

        # Stash list unchanged
        stash_after = _git(tmp_path, "stash", "list").stdout.strip()
        assert stash_after == stash_before

        # Working tree still has the modification
        assert (tmp_path / "file.txt").read_text() == "modified\n"

        # Index unchanged (file not staged)
        status = _git(tmp_path, "diff", "--cached", "--name-only").stdout.strip()
        assert status == ""

    def test_ref_invisible_to_git_log(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("modified\n")
        create_checkpoint(tmp_path, label="hidden")

        log = _git(tmp_path, "log", "--all", "--oneline").stdout
        assert "hidden" not in log
        assert "checkpoint" not in log.lower()

    def test_noop_when_no_changes(self, tmp_path: Path):
        _init_repo(tmp_path)

        result = create_checkpoint(tmp_path, label="empty")

        assert result.created is False
        assert result.reason == "no_changes"

    def test_skips_when_not_git_repo(self, tmp_path: Path):
        (tmp_path / "file.txt").write_text("x\n")

        result = create_checkpoint(tmp_path, label="test")

        assert result.created is False
        assert result.reason == "not_git_repo"

    def test_captures_staged_changes(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "staged.txt").write_text("new file\n")
        _git(tmp_path, "add", "staged.txt")

        result = create_checkpoint(tmp_path, label="staged")

        assert result.created is True

        # Staged file still staged
        staged = _git(tmp_path, "diff", "--cached", "--name-only").stdout.strip()
        assert "staged.txt" in staged

    def test_sanitizes_label(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("changed\n")

        result = create_checkpoint(tmp_path, label="pre push/sync")

        assert result.created is True
        assert "pre-push-sync" in result.ref


class TestListCheckpoints:
    def test_returns_sorted_newest_first(self, tmp_path: Path):
        _init_repo(tmp_path)

        # Create two checkpoints with different timestamps
        (tmp_path / "file.txt").write_text("v1\n")
        with patch("raccoon_cli.checkpoint.time") as mock_time:
            mock_time.time.return_value = 1000000
            create_checkpoint(tmp_path, label="first")

        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "commit v1")

        (tmp_path / "file.txt").write_text("v2\n")
        with patch("raccoon_cli.checkpoint.time") as mock_time:
            mock_time.time.return_value = 2000000
            create_checkpoint(tmp_path, label="second")

        checkpoints = list_checkpoints(tmp_path)

        assert len(checkpoints) == 2
        assert checkpoints[0].label == "second"
        assert checkpoints[1].label == "first"
        assert checkpoints[0].timestamp > checkpoints[1].timestamp

    def test_empty_when_no_checkpoints(self, tmp_path: Path):
        _init_repo(tmp_path)

        assert list_checkpoints(tmp_path) == []

    def test_empty_when_not_git_repo(self, tmp_path: Path):
        assert list_checkpoints(tmp_path) == []


class TestShowCheckpointDiff:
    def test_shows_diff_by_index(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("modified\n")
        create_checkpoint(tmp_path, label="test")

        diff, error = show_checkpoint_diff(tmp_path, "1")

        assert error == ""
        assert diff is not None
        assert "modified" in diff

    def test_shows_diff_by_sha(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("modified\n")
        result = create_checkpoint(tmp_path, label="test")

        diff, error = show_checkpoint_diff(tmp_path, result.short_sha)

        assert error == ""
        assert diff is not None

    def test_error_for_nonexistent(self, tmp_path: Path):
        _init_repo(tmp_path)

        diff, error = show_checkpoint_diff(tmp_path, "999")

        assert diff is None
        assert "not found" in error


class TestRestoreCheckpoint:
    def test_restores_working_tree_changes(self, tmp_path: Path):
        """Checkpoint restores uncommitted changes that were lost."""
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("my-work\n")
        create_checkpoint(tmp_path, label="pre-run")

        # Simulate losing uncommitted changes (e.g. git checkout -- .)
        _git(tmp_path, "checkout", "--", ".")
        assert (tmp_path / "file.txt").read_text() == "initial\n"

        success, error = restore_checkpoint(tmp_path, "1")

        assert success is True
        assert error == ""
        assert (tmp_path / "file.txt").read_text() == "my-work\n"

    def test_restores_new_file(self, tmp_path: Path):
        """Checkpoint restores a new file that was deleted."""
        _init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("important\n")
        create_checkpoint(tmp_path, label="pre-run")

        # Lose the new file
        (tmp_path / "new.txt").unlink()

        success, error = restore_checkpoint(tmp_path, "1")

        assert success is True
        assert (tmp_path / "new.txt").read_text() == "important\n"

    def test_error_for_nonexistent(self, tmp_path: Path):
        _init_repo(tmp_path)

        success, error = restore_checkpoint(tmp_path, "999")

        assert success is False
        assert "not found" in error


class TestDeleteCheckpoint:
    def test_deletes_ref(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("modified\n")
        create_checkpoint(tmp_path, label="doomed")

        assert len(list_checkpoints(tmp_path)) == 1

        success, error = delete_checkpoint(tmp_path, "1")

        assert success is True
        assert error == ""
        assert len(list_checkpoints(tmp_path)) == 0


class TestCleanCheckpoints:
    def test_deletes_all(self, tmp_path: Path):
        _init_repo(tmp_path)

        (tmp_path / "file.txt").write_text("v1\n")
        create_checkpoint(tmp_path, label="a")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "v1")
        (tmp_path / "file.txt").write_text("v2\n")
        create_checkpoint(tmp_path, label="b")

        assert len(list_checkpoints(tmp_path)) == 2

        count, error = clean_checkpoints(tmp_path, delete_all=True)

        assert error == ""
        assert count == 2
        assert len(list_checkpoints(tmp_path)) == 0

    def test_respects_age_threshold(self, tmp_path: Path):
        _init_repo(tmp_path)

        # Create an "old" checkpoint (timestamp in the past)
        (tmp_path / "file.txt").write_text("old\n")
        with patch("raccoon_cli.checkpoint.time") as mock_time:
            mock_time.time.return_value = time.time() - 30 * 86400  # 30 days ago
            create_checkpoint(tmp_path, label="old")

        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "old")

        # Create a "recent" checkpoint
        (tmp_path / "file.txt").write_text("recent\n")
        create_checkpoint(tmp_path, label="recent")

        assert len(list_checkpoints(tmp_path)) == 2

        count, error = clean_checkpoints(tmp_path, max_age_days=7)

        assert error == ""
        assert count == 1
        remaining = list_checkpoints(tmp_path)
        assert len(remaining) == 1
        assert remaining[0].label == "recent"

    def test_noop_when_empty(self, tmp_path: Path):
        _init_repo(tmp_path)

        count, error = clean_checkpoints(tmp_path, delete_all=True)

        assert count == 0
        assert error == ""


class TestPruneExcessCheckpoints:
    def test_prunes_oldest_beyond_limit(self, tmp_path: Path):
        """When more than _MAX_CHECKPOINTS exist, the oldest are deleted."""
        _init_repo(tmp_path)

        limit = 5  # use small limit for fast test

        with patch("raccoon_cli.checkpoint._MAX_CHECKPOINTS", limit):
            for i in range(limit + 3):
                (tmp_path / "file.txt").write_text(f"v{i}\n")
                with patch("raccoon_cli.checkpoint.time") as mock_time:
                    mock_time.time.return_value = 1000000 + i
                    create_checkpoint(tmp_path, label=f"cp-{i}")
                # Commit so the next stash create sees a diff
                _git(tmp_path, "add", "-A")
                _git(tmp_path, "commit", "-m", f"v{i}")

        remaining = list_checkpoints(tmp_path)
        assert len(remaining) == limit

        # The newest checkpoints survived
        labels = [cp.label for cp in remaining]
        for i in range(limit + 3 - limit, limit + 3):
            assert f"cp-{i}" in labels

    def test_noop_under_limit(self, tmp_path: Path):
        """No pruning when checkpoint count is within the limit."""
        _init_repo(tmp_path)

        (tmp_path / "file.txt").write_text("change\n")
        create_checkpoint(tmp_path, label="only-one")

        deleted = _prune_excess_checkpoints(tmp_path)

        assert deleted == 0
        assert len(list_checkpoints(tmp_path)) == 1

    def test_max_checkpoints_constant_is_100(self):
        """Verify the default limit is 100."""
        assert _MAX_CHECKPOINTS == 100
