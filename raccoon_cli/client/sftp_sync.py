"""File synchronization: rsync on Linux/macOS, SFTP on Windows.

This module provides unidirectional synchronization between local project folders
and remote Pi folders. On Linux/macOS rsync is used for efficient delta-transfer.
On Windows SFTP copies every file with a progress bar — cwRsync proved too fragile
(Cygwin path and ACL quirks) to support reliably.

Use ``create_sync()`` to get the right backend automatically.
"""

import fnmatch
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
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
                # Strip trailing slashes for directory patterns (/ and \)
                patterns.append(line.rstrip("/\\"))
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
    update: bool = False  # Skip files that are newer on the destination
    verbose: bool = False  # Print per-file actions
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


# ---------------------------------------------------------------------------
# rsync backend (Linux / macOS)
# ---------------------------------------------------------------------------

class RsyncSync:
    """File synchronization using rsync over SSH."""

    def __init__(self, host: str, user: str = "pi", ssh_port: int = 22):
        self.host = host
        self.user = user
        self.ssh_port = ssh_port

    def sync(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
    ) -> SyncResult:
        options = options or SyncOptions()

        cmd = self._build_command(local_path, remote_path, options)
        logger.debug(f"rsync command: {' '.join(cmd)}")

        try:
            if options.verbose:
                # Stream rsync output in real-time
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout_lines = []
                for line in proc.stdout:
                    stdout_lines.append(line)
                    sys.stdout.write(line)
                    sys.stdout.flush()
                proc.wait(timeout=300)
                stdout = "".join(stdout_lines)
                stderr = proc.stderr.read()
            else:
                proc_result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                stdout = proc_result.stdout
                stderr = proc_result.stderr
                proc = proc_result

            if proc.returncode != 0:
                return SyncResult(
                    success=False,
                    errors=[f"rsync failed (exit {proc.returncode}): {stderr.strip()}"],
                )

            return self._parse_stats(stdout, options.direction)

        except subprocess.TimeoutExpired:
            return SyncResult(
                success=False,
                errors=["rsync timed out after 5 minutes"],
            )
        except Exception as e:
            return SyncResult(success=False, errors=[str(e)])

    def _build_command(
        self,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
    ) -> list[str]:
        cmd = [
            "rsync",
            "-avz",
            "--stats",
            "-e", f"ssh -p {self.ssh_port} -o StrictHostKeyChecking=no",
        ]

        if options.delete:
            cmd.append("--delete")

        if options.update:
            cmd.append("--update")

        for pattern in options.exclude_patterns:
            cmd.extend(["--exclude", pattern])

        remote = f"{self.user}@{self.host}:{remote_path}/"
        local = f"{local_path}/"

        if options.direction == SyncDirection.PUSH:
            cmd.extend([local, remote])
        else:
            cmd.extend([remote, local])

        return cmd

    def _parse_stats(self, output: str, direction: SyncDirection) -> SyncResult:
        result = SyncResult(success=True)

        m = re.search(r"Number of regular files transferred:\s*(\d+)", output)
        transferred = int(m.group(1)) if m else 0

        if direction == SyncDirection.PUSH:
            result.files_uploaded = transferred
        else:
            result.files_downloaded = transferred

        m = re.search(r"Total transferred file size:\s*([\d,]+)", output)
        if m:
            result.bytes_transferred = int(m.group(1).replace(",", ""))

        m = re.search(r"Number of (?:deleted|removed) files:\s*(\d+)", output)
        if m:
            result.files_deleted = int(m.group(1))

        return result


# ---------------------------------------------------------------------------
# SFTP backend (Windows)
# ---------------------------------------------------------------------------

class SftpSync:
    """
    File synchronization using paramiko SFTP.

    Copies every file unconditionally with a progress bar.
    Simple and reliable for the small projects typical in Botball.
    """

    def __init__(self, host: str, user: str = "pi", ssh_port: int = 22):
        self.host = host
        self.user = user
        self.ssh_port = ssh_port

    def sync(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
    ) -> SyncResult:
        options = options or SyncOptions()

        try:
            import paramiko
        except ImportError:
            return SyncResult(
                success=False,
                errors=["paramiko is required for SFTP sync: pip install paramiko"],
            )

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.ssh_port,
                username=self.user,
            )
            sftp = client.open_sftp()

            try:
                if options.direction == SyncDirection.PUSH:
                    return self._push(sftp, local_path, remote_path, options)
                else:
                    return self._pull(sftp, local_path, remote_path, options)
            finally:
                sftp.close()
                client.close()

        except Exception as e:
            return SyncResult(success=False, errors=[str(e)])

    # -- push (local → remote) ----------------------------------------------

    def _push(
        self,
        sftp,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
    ) -> SyncResult:
        from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn

        result = SyncResult(success=True)

        # Collect all files to upload
        files: list[tuple[str, str, str]] = []  # (local_abs, remote_abs, rel_posix)
        for dirpath, dirnames, filenames in os.walk(local_path):
            rel_dir = os.path.relpath(dirpath, local_path)
            if rel_dir == ".":
                rel_dir = ""

            # Prune excluded directories in-place
            dirnames[:] = [
                d for d in dirnames
                if not _should_exclude(
                    f"{rel_dir}/{d}" if rel_dir else d,
                    options.exclude_patterns,
                )
            ]

            for fname in filenames:
                rel_posix = f"{rel_dir}/{fname}" if rel_dir else fname
                rel_posix = rel_posix.replace("\\", "/")
                if _should_exclude(rel_posix, options.exclude_patterns):
                    continue

                local_file = os.path.join(dirpath, fname)
                remote_file = str(PurePosixPath(remote_path) / rel_posix)
                files.append((local_file, remote_file, rel_posix))

        # Create directories and upload with progress bar
        created_dirs: set[str] = set()

        with Progress(
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} files"),
            TransferSpeedColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Uploading", total=len(files))

            for local_file, remote_file, rel_posix in files:
                # Ensure remote directory exists
                remote_dir = str(PurePosixPath(remote_file).parent)
                if remote_dir not in created_dirs:
                    _ensure_remote_dir(sftp, remote_dir, created_dirs)

                local_stat = os.stat(local_file)
                if options.verbose:
                    print(f"  upload: {rel_posix} ({local_stat.st_size} bytes)")
                sftp.put(local_file, remote_file)
                result.files_uploaded += 1
                result.bytes_transferred += local_stat.st_size
                progress.advance(task)

        # Delete remote files not present locally (skip excluded patterns)
        pushed_set = {f[2] for f in files}
        if options.delete:
            remote_files = _list_remote_recursive(sftp, remote_path)
            for rf in remote_files:
                if rf not in pushed_set and not _should_exclude(rf, options.exclude_patterns):
                    try:
                        if options.verbose:
                            print(f"  delete (remote): {rf}")
                        sftp.remove(str(PurePosixPath(remote_path) / rf))
                        result.files_deleted += 1
                    except Exception:
                        pass

        return result

    # -- pull (remote → local) ----------------------------------------------

    def _pull(
        self,
        sftp,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
    ) -> SyncResult:
        from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn

        result = SyncResult(success=True)
        pulled_rel: set[str] = set()

        remote_files = _list_remote_recursive(sftp, remote_path)
        # Filter excluded
        remote_files = [
            rf for rf in remote_files
            if not _should_exclude(rf, options.exclude_patterns)
        ]

        with Progress(
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} files"),
            TransferSpeedColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Downloading", total=len(remote_files))

            for rel_file in remote_files:
                remote_file = str(PurePosixPath(remote_path) / rel_file)
                local_file = local_path / rel_file.replace("/", os.sep)
                local_file.parent.mkdir(parents=True, exist_ok=True)

                # Skip files that are newer locally (user edits during run)
                if options.update and local_file.exists():
                    local_mtime = local_file.stat().st_mtime
                    try:
                        remote_mtime = sftp.stat(remote_file).st_mtime
                    except Exception:
                        remote_mtime = 0
                    if local_mtime > remote_mtime:
                        if options.verbose:
                            print(f"  skip (newer local): {rel_file}")
                        progress.advance(task)
                        continue

                if options.verbose:
                    print(f"  download: {rel_file}")
                sftp.get(remote_file, str(local_file))
                result.files_downloaded += 1
                result.bytes_transferred += local_file.stat().st_size
                pulled_rel.add(rel_file)
                progress.advance(task)

        if options.delete:
            remote_set = set(remote_files)
            for dirpath, _dirnames, filenames in os.walk(local_path):
                rel_dir = os.path.relpath(dirpath, local_path)
                if rel_dir == ".":
                    rel_dir = ""
                for fname in filenames:
                    rel_posix = f"{rel_dir}/{fname}" if rel_dir else fname
                    rel_posix = rel_posix.replace("\\", "/")
                    if rel_posix not in remote_set and not _should_exclude(rel_posix, options.exclude_patterns):
                        try:
                            if options.verbose:
                                print(f"  delete (local): {rel_posix}")
                            os.remove(os.path.join(dirpath, fname))
                            result.files_deleted += 1
                        except Exception:
                            pass

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _should_exclude(rel_path: str, patterns: list[str]) -> bool:
    """Check whether *rel_path* matches any exclude pattern.

    Each component of the path is tested against the patterns so that a
    pattern like ``__pycache__`` matches ``src/__pycache__/foo.pyc``.
    """
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts:
        for pat in patterns:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _ensure_remote_dir(sftp, path: str, cache: set[str]) -> None:
    """Create *path* on the remote side, recursively.

    Uses the provided *cache* set (per-sync-session) to avoid redundant stat calls.
    """
    if path in cache or path in ("", "/"):
        return
    parts = PurePosixPath(path).parts
    for i in range(1, len(parts) + 1):
        partial = str(PurePosixPath(*parts[:i]))
        if partial in cache:
            continue
        try:
            sftp.stat(partial)
        except FileNotFoundError:
            sftp.mkdir(partial)
        cache.add(partial)


def _list_remote_recursive(sftp, remote_root: str) -> list[str]:
    """Return a list of relative POSIX paths of all regular files under *remote_root*."""
    result: list[str] = []
    dirs = [""]
    while dirs:
        rel = dirs.pop()
        abs_dir = str(PurePosixPath(remote_root) / rel) if rel else remote_root
        try:
            entries = sftp.listdir_attr(abs_dir)
        except Exception:
            continue
        for entry in entries:
            child_rel = f"{rel}/{entry.filename}" if rel else entry.filename
            if stat.S_ISDIR(entry.st_mode):
                dirs.append(child_rel)
            else:
                result.append(child_rel)
    return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_sync(host: str, user: str = "pi", ssh_port: int = 22):
    """Return the best available sync backend.

    Uses rsync on Linux/macOS, SFTP on Windows. rsync is skipped on Windows
    even if cwRsync is on PATH — the Cygwin bundle's path and ssh-key handling
    are too unreliable to depend on.
    """
    if sys.platform != "win32" and shutil.which("rsync"):
        logger.info("Using rsync backend for file sync")
        return RsyncSync(host=host, user=user, ssh_port=ssh_port)
    logger.info("Using SFTP backend for file sync")
    return SftpSync(host=host, user=user, ssh_port=ssh_port)
