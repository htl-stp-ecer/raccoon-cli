"""Tests for rsync-based sync functionality."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from raccoon.client.sftp_sync import (
    RcloneSync,
    SyncOptions,
    SyncDirection,
    SyncResult,
    load_raccoonignore,
)


class TestRsyncCommandConstruction:
    """Test that rsync commands are built correctly."""

    def test_push_command(self):
        """Push should put local path first, remote path second."""
        sync = RcloneSync(host="192.168.4.1", user="pi")
        options = SyncOptions(direction=SyncDirection.PUSH, delete=False)

        cmd = sync._build_command(Path("/home/user/project"), "/home/pi/programs/abc", options)

        assert cmd[0] == "rsync"
        assert "-avz" in cmd
        assert "--stats" in cmd
        # Source (local) before destination (remote)
        assert cmd[-2] == "/home/user/project/"
        assert cmd[-1] == "pi@192.168.4.1:/home/pi/programs/abc/"

    def test_pull_command(self):
        """Pull should put remote path first, local path second."""
        sync = RcloneSync(host="192.168.4.1", user="pi")
        options = SyncOptions(direction=SyncDirection.PULL, delete=False)

        cmd = sync._build_command(Path("/home/user/project"), "/home/pi/programs/abc", options)

        # Source (remote) before destination (local)
        assert cmd[-2] == "pi@192.168.4.1:/home/pi/programs/abc/"
        assert cmd[-1] == "/home/user/project/"

    def test_delete_flag(self):
        """--delete should be included when delete=True."""
        sync = RcloneSync(host="192.168.4.1", user="pi")

        options_del = SyncOptions(delete=True)
        cmd_del = sync._build_command(Path("/tmp/proj"), "/remote", options_del)
        assert "--delete" in cmd_del

        options_nodel = SyncOptions(delete=False)
        cmd_nodel = sync._build_command(Path("/tmp/proj"), "/remote", options_nodel)
        assert "--delete" not in cmd_nodel

    def test_ssh_port(self):
        """Custom SSH port should be passed via -e flag."""
        sync = RcloneSync(host="192.168.4.1", user="pi", ssh_port=2222)
        options = SyncOptions(delete=False)

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        # Find the -e argument
        e_idx = cmd.index("-e")
        ssh_cmd = cmd[e_idx + 1]
        assert "-p 2222" in ssh_cmd

    def test_default_ssh_port(self):
        """Default SSH port should be 22."""
        sync = RcloneSync(host="192.168.4.1", user="pi")
        options = SyncOptions(delete=False)

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        e_idx = cmd.index("-e")
        ssh_cmd = cmd[e_idx + 1]
        assert "-p 22" in ssh_cmd

    def test_custom_user(self):
        """Custom user should appear in remote path."""
        sync = RcloneSync(host="10.0.0.1", user="admin")
        options = SyncOptions(direction=SyncDirection.PUSH, delete=False)

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        assert cmd[-1] == "admin@10.0.0.1:/remote/"


class TestExcludePatterns:
    """Test exclude pattern handling."""

    def test_default_exclude_patterns(self):
        """Default options should include common exclude patterns."""
        options = SyncOptions()

        assert ".git" in options.exclude_patterns
        assert "__pycache__" in options.exclude_patterns
        assert "*.pyc" in options.exclude_patterns
        assert ".raccoon" in options.exclude_patterns

    def test_exclude_patterns_in_command(self):
        """Each exclude pattern should become an --exclude argument."""
        sync = RcloneSync(host="192.168.4.1", user="pi")
        options = SyncOptions(
            exclude_patterns=["*.pyc", ".git", "__pycache__"],
            delete=False,
        )

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        # Count --exclude occurrences
        exclude_indices = [i for i, arg in enumerate(cmd) if arg == "--exclude"]
        assert len(exclude_indices) == 3

        # Check the patterns follow --exclude
        exclude_values = [cmd[i + 1] for i in exclude_indices]
        assert "*.pyc" in exclude_values
        assert ".git" in exclude_values
        assert "__pycache__" in exclude_values

    def test_additional_exclude_patterns(self):
        """Additional patterns should be appended to defaults."""
        options = SyncOptions()
        options.exclude_patterns = options.exclude_patterns + ["*.tmp", "build"]

        assert "*.tmp" in options.exclude_patterns
        assert "build" in options.exclude_patterns
        # Originals still present
        assert ".git" in options.exclude_patterns


class TestRaccoonIgnore:
    """Test .raccoonignore file loading."""

    def test_load_raccoonignore(self, tmp_path: Path):
        """Should load patterns from .raccoonignore file."""
        ignore_file = tmp_path / ".raccoonignore"
        ignore_file.write_text("*.tmp\n# comment\n\nbuild\ndata/\n")

        patterns = load_raccoonignore(tmp_path)

        assert "*.tmp" in patterns
        assert "build" in patterns
        assert "data" in patterns  # trailing slash stripped
        assert len(patterns) == 3  # comments and blank lines excluded

    def test_missing_raccoonignore(self, tmp_path: Path):
        """Missing .raccoonignore should return empty list."""
        patterns = load_raccoonignore(tmp_path)
        assert patterns == []


class TestRsyncNotFound:
    """Test behavior when rsync is not installed."""

    @patch("raccoon.client.sftp_sync.shutil.which", return_value=None)
    def test_rsync_not_found_returns_error(self, mock_which):
        """Should return failure with helpful message when rsync is missing."""
        sync = RcloneSync(host="192.168.4.1", user="pi")

        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is False
        assert len(result.errors) == 1
        assert "rsync not found" in result.errors[0]
        assert "sudo apt install rsync" in result.errors[0]


class TestRsyncExecution:
    """Test rsync execution and result parsing."""

    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_successful_push(self, mock_run, mock_which):
        """Successful push should parse stats correctly."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Number of files: 10\n"
                "Number of regular files transferred: 3\n"
                "Total transferred file size: 4,096 bytes\n"
            ),
            stderr="",
        )

        sync = RcloneSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is True
        assert result.files_uploaded == 3
        assert result.files_downloaded == 0
        assert result.bytes_transferred == 4096

    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_successful_pull(self, mock_run, mock_which):
        """Successful pull should count files as downloaded."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Number of regular files transferred: 5\n"
                "Total transferred file size: 10,240 bytes\n"
            ),
            stderr="",
        )

        sync = RcloneSync(host="192.168.4.1", user="pi")
        options = SyncOptions(direction=SyncDirection.PULL)
        result = sync.sync(Path("/tmp/proj"), "/remote", options)

        assert result.success is True
        assert result.files_downloaded == 5
        assert result.files_uploaded == 0

    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_rsync_failure(self, mock_run, mock_which):
        """Non-zero exit code should return failure."""
        mock_run.return_value = MagicMock(
            returncode=12,
            stdout="",
            stderr="rsync error: some error occurred",
        )

        sync = RcloneSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is False
        assert "exit 12" in result.errors[0]

    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_deleted_files_parsed(self, mock_run, mock_which):
        """Deleted file count should be parsed from stats."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Number of regular files transferred: 1\n"
                "Number of deleted files: 2\n"
                "Total transferred file size: 100 bytes\n"
            ),
            stderr="",
        )

        sync = RcloneSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.files_deleted == 2

    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_nothing_transferred(self, mock_run, mock_which):
        """Zero transfers should still be success."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Number of regular files transferred: 0\n"
                "Total transferred file size: 0 bytes\n"
            ),
            stderr="",
        )

        sync = RcloneSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is True
        assert result.files_uploaded == 0
        assert result.files_downloaded == 0

    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("raccoon.client.sftp_sync.subprocess.run", side_effect=TimeoutError)
    def test_timeout_handling(self, mock_run, mock_which):
        """Timeout should be caught and reported."""
        from subprocess import TimeoutExpired

        mock_run.side_effect = TimeoutExpired("rsync", 300)

        sync = RcloneSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is False
        assert "timed out" in result.errors[0]


class TestSyncOptions:
    """Test sync options configuration."""

    def test_default_options(self):
        """Default options should be sensible."""
        options = SyncOptions()

        assert options.direction == SyncDirection.PUSH
        assert options.delete is True
        assert ".git" in options.exclude_patterns
        assert "__pycache__" in options.exclude_patterns
        assert ".raccoon" in options.exclude_patterns

    def test_pull_direction(self):
        """Should be able to set pull direction."""
        options = SyncOptions(direction=SyncDirection.PULL)
        assert options.direction == SyncDirection.PULL
