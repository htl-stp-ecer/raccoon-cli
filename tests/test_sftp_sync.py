"""Tests for sync functionality (rsync + SFTP)."""

import os
import sys
import stat as stat_mod
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from raccoon.client.sftp_sync import (
    RsyncSync,
    SftpSync,
    create_sync,
    SyncOptions,
    SyncDirection,
    SyncResult,
    load_raccoonignore,
    _should_exclude,
)


# ── RsyncSync command construction ────────────────────────────────────────

class TestRsyncCommandConstruction:
    """Test that rsync commands are built correctly."""

    def test_push_command(self):
        """Push should put local path first, remote path second."""
        sync = RsyncSync(host="192.168.4.1", user="pi")
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
        sync = RsyncSync(host="192.168.4.1", user="pi")
        options = SyncOptions(direction=SyncDirection.PULL, delete=False)

        cmd = sync._build_command(Path("/home/user/project"), "/home/pi/programs/abc", options)

        # Source (remote) before destination (local)
        assert cmd[-2] == "pi@192.168.4.1:/home/pi/programs/abc/"
        assert cmd[-1] == "/home/user/project/"

    def test_delete_flag(self):
        """--delete should be included when delete=True."""
        sync = RsyncSync(host="192.168.4.1", user="pi")

        options_del = SyncOptions(delete=True)
        cmd_del = sync._build_command(Path("/tmp/proj"), "/remote", options_del)
        assert "--delete" in cmd_del

        options_nodel = SyncOptions(delete=False)
        cmd_nodel = sync._build_command(Path("/tmp/proj"), "/remote", options_nodel)
        assert "--delete" not in cmd_nodel

    def test_ssh_port(self):
        """Custom SSH port should be passed via -e flag."""
        sync = RsyncSync(host="192.168.4.1", user="pi", ssh_port=2222)
        options = SyncOptions(delete=False)

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        # Find the -e argument
        e_idx = cmd.index("-e")
        ssh_cmd = cmd[e_idx + 1]
        assert "-p 2222" in ssh_cmd

    def test_default_ssh_port(self):
        """Default SSH port should be 22."""
        sync = RsyncSync(host="192.168.4.1", user="pi")
        options = SyncOptions(delete=False)

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        e_idx = cmd.index("-e")
        ssh_cmd = cmd[e_idx + 1]
        assert "-p 22" in ssh_cmd

    def test_custom_user(self):
        """Custom user should appear in remote path."""
        sync = RsyncSync(host="10.0.0.1", user="admin")
        options = SyncOptions(direction=SyncDirection.PUSH, delete=False)

        cmd = sync._build_command(Path("/tmp/proj"), "/remote", options)

        assert cmd[-1] == "admin@10.0.0.1:/remote/"


# ── Exclude patterns ─────────────────────────────────────────────────────

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
        sync = RsyncSync(host="192.168.4.1", user="pi")
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


# ── .raccoonignore ────────────────────────────────────────────────────────

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

    def test_strips_backslash_suffix(self, tmp_path: Path):
        """Trailing backslash should also be stripped (Windows editors)."""
        ignore_file = tmp_path / ".raccoonignore"
        ignore_file.write_text("build\\\n")

        patterns = load_raccoonignore(tmp_path)
        assert "build" in patterns


# ── RsyncSync execution + parsing ────────────────────────────────────────

class TestRsyncExecution:
    """Test rsync execution and result parsing."""

    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_successful_push(self, mock_run):
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

        sync = RsyncSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is True
        assert result.files_uploaded == 3
        assert result.files_downloaded == 0
        assert result.bytes_transferred == 4096

    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_successful_pull(self, mock_run):
        """Successful pull should count files as downloaded."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Number of regular files transferred: 5\n"
                "Total transferred file size: 10,240 bytes\n"
            ),
            stderr="",
        )

        sync = RsyncSync(host="192.168.4.1", user="pi")
        options = SyncOptions(direction=SyncDirection.PULL)
        result = sync.sync(Path("/tmp/proj"), "/remote", options)

        assert result.success is True
        assert result.files_downloaded == 5
        assert result.files_uploaded == 0

    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_rsync_failure(self, mock_run):
        """Non-zero exit code should return failure."""
        mock_run.return_value = MagicMock(
            returncode=12,
            stdout="",
            stderr="rsync error: some error occurred",
        )

        sync = RsyncSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is False
        assert "exit 12" in result.errors[0]

    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_deleted_files_parsed(self, mock_run):
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

        sync = RsyncSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.files_deleted == 2

    @patch("raccoon.client.sftp_sync.subprocess.run")
    def test_nothing_transferred(self, mock_run):
        """Zero transfers should still be success."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Number of regular files transferred: 0\n"
                "Total transferred file size: 0 bytes\n"
            ),
            stderr="",
        )

        sync = RsyncSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is True
        assert result.files_uploaded == 0
        assert result.files_downloaded == 0

    @patch("raccoon.client.sftp_sync.subprocess.run", side_effect=TimeoutError)
    def test_timeout_handling(self, mock_run):
        """Timeout should be caught and reported."""
        from subprocess import TimeoutExpired

        mock_run.side_effect = TimeoutExpired("rsync", 300)

        sync = RsyncSync(host="192.168.4.1", user="pi")
        result = sync.sync(Path("/tmp/proj"), "/remote")

        assert result.success is False
        assert "timed out" in result.errors[0]


# ── SyncOptions ───────────────────────────────────────────────────────────

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


# ── _should_exclude helper ────────────────────────────────────────────────

class TestShouldExclude:
    """Test the _should_exclude helper."""

    def test_direct_match(self):
        assert _should_exclude(".git", [".git"]) is True

    def test_nested_match(self):
        assert _should_exclude("src/__pycache__/foo.pyc", ["__pycache__"]) is True

    def test_glob_match(self):
        assert _should_exclude("module.pyc", ["*.pyc"]) is True

    def test_no_match(self):
        assert _should_exclude("src/main.py", [".git", "*.pyc"]) is False

    def test_windows_backslash(self):
        """Backslash paths should be normalized and matched."""
        assert _should_exclude("src\\__pycache__\\foo.pyc", ["__pycache__"]) is True


# ── SftpSync ──────────────────────────────────────────────────────────────

class TestSftpSync:
    """Test the paramiko SFTP fallback backend."""

    def _make_mock_paramiko(self):
        """Build mock paramiko module, SSHClient, and SFTP channel."""
        mock_sftp = MagicMock()
        mock_client = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp

        mock_paramiko = MagicMock()
        mock_paramiko.SSHClient.return_value = mock_client
        mock_paramiko.AutoAddPolicy.return_value = "policy"
        return mock_paramiko, mock_client, mock_sftp

    def test_push_uploads_files(self, tmp_path):
        """Push should sftp.put every non-excluded file."""
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "lib.py").write_text("x=1")

        mock_paramiko, mock_client, mock_sftp = self._make_mock_paramiko()

        with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
            sync = SftpSync(host="192.168.4.1", user="pi")
            options = SyncOptions(direction=SyncDirection.PUSH, delete=False, exclude_patterns=[])
            result = sync.sync(tmp_path, "/home/pi/proj", options)

        assert result.success is True
        assert result.files_uploaded == 2
        assert result.bytes_transferred > 0
        assert mock_sftp.put.call_count == 2

    def test_push_excludes_patterns(self, tmp_path):
        """Push should skip files matching exclude patterns."""
        (tmp_path / "main.py").write_text("ok")
        (tmp_path / "debug.log").write_text("noise")

        mock_paramiko, mock_client, mock_sftp = self._make_mock_paramiko()

        with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
            sync = SftpSync(host="192.168.4.1", user="pi")
            options = SyncOptions(direction=SyncDirection.PUSH, delete=False, exclude_patterns=["*.log"])
            result = sync.sync(tmp_path, "/home/pi/proj", options)

        assert result.files_uploaded == 1  # only main.py

    def test_push_deletes_remote_extras(self, tmp_path):
        """Push with delete=True should remove remote files not present locally."""
        (tmp_path / "main.py").write_text("ok")

        mock_paramiko, mock_client, mock_sftp = self._make_mock_paramiko()

        # Remote has an extra file
        extra_attr = MagicMock()
        extra_attr.filename = "old.py"
        extra_attr.st_mode = 0o100644  # regular file
        mock_sftp.listdir_attr.return_value = [extra_attr]

        with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
            sync = SftpSync(host="192.168.4.1", user="pi")
            options = SyncOptions(direction=SyncDirection.PUSH, delete=True, exclude_patterns=[])
            result = sync.sync(tmp_path, "/home/pi/proj", options)

        assert result.files_deleted == 1
        mock_sftp.remove.assert_called_once()

    def test_pull_downloads_files(self, tmp_path):
        """Pull should sftp.get remote files to local."""
        mock_paramiko, mock_client, mock_sftp = self._make_mock_paramiko()

        # Mock remote listing with one file
        remote_attr = MagicMock()
        remote_attr.filename = "main.py"
        remote_attr.st_mode = 0o100644  # regular file
        mock_sftp.listdir_attr.return_value = [remote_attr]

        def fake_get(remote, local):
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_text("remote content")

        mock_sftp.get.side_effect = fake_get

        with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
            sync = SftpSync(host="192.168.4.1", user="pi")
            options = SyncOptions(direction=SyncDirection.PULL, delete=False, exclude_patterns=[])
            result = sync.sync(tmp_path, "/home/pi/proj", options)

        assert result.success is True
        assert result.files_downloaded == 1
        assert (tmp_path / "main.py").exists()

    def test_missing_paramiko_returns_error(self):
        """Should return helpful error when paramiko is not installed."""
        sync = SftpSync(host="192.168.4.1", user="pi")

        # Temporarily hide paramiko from import system
        real_paramiko = sys.modules.get("paramiko")
        sys.modules["paramiko"] = None  # causes ImportError on `import paramiko`
        try:
            result = sync.sync(Path("/tmp/proj"), "/remote")
        finally:
            if real_paramiko is not None:
                sys.modules["paramiko"] = real_paramiko
            else:
                del sys.modules["paramiko"]

        assert result.success is False
        assert "paramiko" in result.errors[0]


# ── create_sync factory ──────────────────────────────────────────────────

class TestCreateSync:
    """Test the create_sync factory function."""

    @patch("raccoon.client.sftp_sync.sys")
    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    def test_returns_rsync_on_linux(self, mock_which, mock_sys):
        mock_sys.platform = "linux"
        sync = create_sync(host="192.168.4.1", user="pi")
        assert isinstance(sync, RsyncSync)

    @patch("raccoon.client.sftp_sync.sys")
    @patch("raccoon.client.sftp_sync.shutil.which", return_value=None)
    def test_returns_sftp_when_rsync_missing(self, mock_which, mock_sys):
        mock_sys.platform = "linux"
        sync = create_sync(host="192.168.4.1", user="pi", ssh_port=2222)
        assert isinstance(sync, SftpSync)
        assert sync.ssh_port == 2222

    @patch("raccoon.client.sftp_sync.sys")
    @patch("raccoon.client.sftp_sync.shutil.which", return_value="/usr/bin/rsync")
    def test_returns_sftp_on_windows(self, mock_which, mock_sys):
        """Windows should always use SFTP even if rsync is somehow on PATH."""
        mock_sys.platform = "win32"
        sync = create_sync(host="192.168.4.1", user="pi")
        assert isinstance(sync, SftpSync)
