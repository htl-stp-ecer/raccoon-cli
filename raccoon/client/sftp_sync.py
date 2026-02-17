import logging
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
                if not line or line.startswith("#"):
                    continue
                patterns.append(line.rstrip("/"))
    except Exception:
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
    delete: bool = True
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


class RcloneSync:
    """
    File synchronization using rclone over SFTP.

    Features:
    - Incremental transfer (only changed files)
    - Optional deletion of extraneous files
    - Exclusion patterns
    - Progress reporting
    """

    def __init__(self, host: str, user: str = "pi", ssh_port: int = 22):
        """
        Initialize the rclone sync client.

        Args:
            host: Remote hostname or IP address
            user: Username on the remote SFTP server
            port: SFTP port number
        """
        self.host = host
        self.user = user
        self.port = ssh_port

    def sync(
            self,
            local_path: Path,
            remote_path: str,
            options: Optional[SyncOptions] = None,
    ) -> SyncResult:
        """
        Sync files between local and remote directories using rclone.

        Args:
            local_path: Local directory to sync
            remote_path: Remote directory path
            options: Sync options including direction and exclusions

        Returns:
            SyncResult with statistics
        """
        options = options or SyncOptions()

        if not shutil.which("rclone"):
            return SyncResult(
                success=False,
                errors=[
                    "rclone not found. Install it here: https://rclone.org/install/#windows"
                ],
            )

        cmd = self._build_command(local_path, remote_path, options)
        logger.debug(f"rclone command: {' '.join(cmd)}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )

            if proc.returncode != 0:
                return SyncResult(
                    success=False,
                    errors=[f"rclone failed (exit {proc.returncode}): {proc.stderr.strip()}"],
                )

            return self._parse_stats(proc.stdout, options.direction)

        except subprocess.TimeoutExpired:
            return SyncResult(
                success=False,
                errors=["rclone timed out after 10 minutes"],
            )
        except Exception as e:
            return SyncResult(success=False, errors=[str(e)])

    def _build_command(
            self,
            local_path: Path,
            remote_path: str,
            options: SyncOptions,
    ) -> list[str]:
        cmd = ["rclone", "sync"]

        for pattern in options.exclude_patterns:
            cmd.extend(['--exclude', pattern])

        local = f"{local_path}/"
        remote = f":sftp,host={self.host},user={self.user},port={self.port}:{remote_path}"

        if options.direction == SyncDirection.PUSH:
            cmd.extend([local, remote])
        else:
            cmd.extend([remote, local])

        cmd.append("--progress")
        cmd.append("-c")
        return cmd

    def _parse_stats(self, output: str, direction: SyncDirection) -> SyncResult:
        result = SyncResult(success=True)
        transferred_bytes = 0
        files_transferred = 0
        files_deleted = 0

        lines = [line.strip() for line in output.splitlines() if line.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]

            if line.startswith("Transferred:") and transferred_bytes == 0:
                parts = line.split(",")
                first_part = parts[0].replace("Transferred:", "").strip()
                if "/" in first_part:
                    bytes_str = first_part.split("/")[0].strip()
                    transferred_bytes = self._parse_size(bytes_str)
                i += 1
                continue

            if line.startswith("Transferred:") and files_transferred == 0 and "100%" in line:
                first_part = line.replace("Transferred:", "").strip()
                if "/" in first_part:
                    try:
                        files_transferred = int(first_part.split("/")[0].strip())
                    except Exception:
                        files_transferred = 0
                i += 1
                continue

            if line.startswith("Deleted Files:"):
                try:
                    files_deleted = int(line.split(":")[1].strip())
                except Exception:
                    files_deleted = 0

            i += 1

        if direction == SyncDirection.PUSH:
            result.files_uploaded = files_transferred
        else:
            result.files_downloaded = files_transferred

        result.bytes_transferred = transferred_bytes
        result.files_deleted = files_deleted
        return result


    def _parse_size(self, size_str: str) -> int:
        size_str = size_str.strip().upper()
        if size_str.endswith("K"):
            return int(float(size_str[:-1]) * 1024)
        elif size_str.endswith("M"):
            return int(float(size_str[:-1]) * 1024 ** 2)
        elif size_str.endswith("G"):
            return int(float(size_str[:-1]) * 1024 ** 3)
        elif size_str.endswith("T"):
            return int(float(size_str[:-1]) * 1024 ** 4)
        elif size_str.endswith("B"):
            return int(float(size_str[:-1]))
        else:
            return int(float(size_str))
