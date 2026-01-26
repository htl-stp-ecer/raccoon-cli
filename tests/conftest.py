"""Pytest fixtures for raccoon tests."""

import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory structure."""
    # Create basic project structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n")
    (tmp_path / "raccoon.project.yml").write_text("name: test-project\nuuid: test-uuid\n")
    return tmp_path


@pytest.fixture
def mock_sftp():
    """Create a mock SFTP client."""
    sftp = MagicMock()

    # Mock file operations
    sftp.stat.return_value = MagicMock(st_mode=0o100644, st_size=100, st_mtime=1000)
    sftp.listdir_attr.return_value = []

    return sftp


@pytest.fixture
def mock_ssh_client(mock_sftp):
    """Create a mock SSH client that returns the mock SFTP."""
    ssh = MagicMock()
    ssh.open_sftp.return_value = mock_sftp
    return ssh
