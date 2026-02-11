"""Rsync-based file synchronization.

This module provides unidirectional synchronization between local project folders
and remote Pi folders using rsync over SSH.
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("raccoon")


class SyncDirection(Enum):
    """Direction of synchronization."""

    PUSH = "push"  # Local -> Remote
    PULL = "pull"  # Remote -> Local


def load_raccoonignore(project_root: Path) -> list[str]:
    """
    Load ignore patterns from .raccoonignore file.

    The .raccoonignore file supports:
    - One pattern per line
    - Lines starting with # are comments
    - Empty lines are ignored
    - Patterns use fnmatch/glob syntax (e.g., *.pyc, __pycache__, defs/)

    Args:
        project_root: Path to the project root directory

    Returns:
        List of additional exclude patterns from .raccoonignore
    """
    ignore_file = project_root / ".raccoonignore"
    patterns = []

    if not ignore_file.exists():
        return patterns

    try:
        with open(ignore_file, "r") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                # Strip trailing slashes for directory patterns
                patterns.append(line.rstrip("/"))
    except Exception:
        # If we can't read the file, just return empty list
        pass

    return patterns


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    files_uploaded: int = 0
    files_downloaded: int = 0
    files_deleted: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncOptions:
    """Options for sync operation."""

    direction: SyncDirection = SyncDirection.PUSH
    delete: bool = True  # Delete extraneous files on destination
    exclude_patterns: list[str] = field(
        default_factory=lambda: [
            ".git",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            ".idea",
            ".vscode",
            "venv",
            ".venv",
            "*.egg-info",
            ".pytest_cache",
            ".mypy_cache",
            "*.log",
            ".DS_Store",
            ".raccoon",
        ]
    )


class RsyncSync:
    """
    File synchronization using rsync over SSH.

    Features:
    - Efficient delta-transfer algorithm
    - Proper change detection (timestamps + checksums)
    - Exclusion patterns
    - Optional deletion of extraneous files
    - Progress reporting
    """

    def __init__(self, host: str, user: str = "pi", ssh_port: int = 22):
        """
        Initialize the rsync sync client.

        Args:
            host: Remote hostname or IP address
            user: SSH username
            ssh_port: SSH port number
        """
        self.host = host
        self.user = user
        self.ssh_port = ssh_port

    def sync(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
    ) -> SyncResult:
        """
        Sync files between local and remote directories using rsync.

        Args:
            local_path: Local directory to sync
            remote_path: Remote directory path on Pi
            options: Sync options including direction and exclusions

        Returns:
            SyncResult with statistics
        """
        options = options or SyncOptions()

        # Check rsync is available
        if not shutil.which("rsync"):
            return SyncResult(
                success=False,
                errors=[
                    "rsync not found. Install it with:\n"
                    "  Linux:  sudo apt install rsync\n"
                    "  macOS:  brew install rsync\n"
                    "  Windows: use WSL or Git Bash"
                ],
            )

        cmd = self._build_command(local_path, remote_path, options)
        logger.debug(f"rsync command: {' '.join(cmd)}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if proc.returncode != 0:
                return SyncResult(
                    success=False,
                    errors=[f"rsync failed (exit {proc.returncode}): {proc.stderr.strip()}"],
                )

            return self._parse_stats(proc.stdout, options.direction)

        except subprocess.TimeoutExpired:
            return SyncResult(
                success=False,
                errors=["rsync timed out after 5 minutes"],
            )
        except Exception as e:
            return SyncResult(
                success=False,
                errors=[str(e)],
            )

    def _build_command(
        self,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
    ) -> list[str]:
        """Build the rsync command line."""
        cmd = [
            "rsync",
            "-avz",
            "--stats",
            "-e", f"ssh -p {self.ssh_port} -o StrictHostKeyChecking=no",
        ]

        if options.delete:
            cmd.append("--delete")

        for pattern in options.exclude_patterns:
            cmd.extend(["--exclude", pattern])

        remote = f"{self.user}@{self.host}:{remote_path}/"

        # Trailing slash on source means "contents of directory"
        local = f"{local_path}/"

        if options.direction == SyncDirection.PUSH:
            cmd.extend([local, remote])
        else:  # PULL
            cmd.extend([remote, local])

        return cmd

    def _parse_stats(self, output: str, direction: SyncDirection) -> SyncResult:
        """Parse rsync --stats output for transfer counts."""
        result = SyncResult(success=True)

        # "Number of regular files transferred: 5"
        m = re.search(r"Number of regular files transferred:\s*(\d+)", output)
        transferred = int(m.group(1)) if m else 0

        if direction == SyncDirection.PUSH:
            result.files_uploaded = transferred
        else:
            result.files_downloaded = transferred

        # "Total transferred file size: 1,234 bytes"
        m = re.search(r"Total transferred file size:\s*([\d,]+)", output)
        if m:
            result.bytes_transferred = int(m.group(1).replace(",", ""))

        # "Number of deleted files: 3" (rsync 3.1+)
        m = re.search(r"Number of (?:deleted|removed) files:\s*(\d+)", output)
        if m:
            result.files_deleted = int(m.group(1))

        return result
