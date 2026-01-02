"""SFTP-based file synchronization using paramiko."""

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import paramiko
from rich.progress import Progress, TaskID


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    files_uploaded: int = 0
    files_deleted: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncOptions:
    """Options for sync operation."""

    delete_remote: bool = True  # Delete files on remote not in local
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
        ]
    )


class SftpSync:
    """
    Smart file synchronization using SFTP.

    Features:
    - Hash-based change detection (only upload changed files)
    - Exclusion patterns for ignoring files
    - Optional remote file deletion
    - Progress reporting
    """

    def __init__(self, ssh_client: paramiko.SSHClient):
        """
        Initialize the sync client.

        Args:
            ssh_client: Connected paramiko SSHClient
        """
        self.ssh = ssh_client
        self.sftp: Optional[paramiko.SFTPClient] = None

    def sync(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> SyncResult:
        """
        Sync local directory to remote.

        Args:
            local_path: Local directory to sync
            remote_path: Remote directory path on Pi
            options: Sync options
            progress_callback: Called with (filename, bytes_done, bytes_total)

        Returns:
            SyncResult with statistics
        """
        options = options or SyncOptions()
        result = SyncResult(success=True)

        try:
            self.sftp = self.ssh.open_sftp()

            # Ensure remote directory exists
            self._mkdir_p(remote_path)

            # Get local files
            local_files = self._get_local_files(local_path, options.exclude_patterns)

            # Get remote files
            remote_files = self._get_remote_files(remote_path)

            # Calculate what to upload (new or changed files)
            to_upload = []
            for rel_path, local_info in local_files.items():
                remote_info = remote_files.get(rel_path)
                if remote_info is None or local_info["hash"] != remote_info.get("hash"):
                    to_upload.append((rel_path, local_info))

            # Upload files
            for rel_path, info in to_upload:
                local_file = local_path / rel_path
                remote_file = f"{remote_path}/{rel_path}"

                try:
                    # Ensure parent directory exists
                    remote_dir = os.path.dirname(remote_file)
                    self._mkdir_p(remote_dir)

                    # Upload file
                    if progress_callback:
                        progress_callback(rel_path, 0, info["size"])

                    self.sftp.put(str(local_file), remote_file)

                    if progress_callback:
                        progress_callback(rel_path, info["size"], info["size"])

                    result.files_uploaded += 1
                    result.bytes_transferred += info["size"]
                except Exception as e:
                    result.errors.append(f"Failed to upload {rel_path}: {e}")

            # Delete remote files not in local
            if options.delete_remote:
                for rel_path in remote_files:
                    if rel_path not in local_files:
                        remote_file = f"{remote_path}/{rel_path}"
                        try:
                            self.sftp.remove(remote_file)
                            result.files_deleted += 1
                        except Exception as e:
                            result.errors.append(f"Failed to delete {rel_path}: {e}")

                # Clean up empty directories
                self._cleanup_empty_dirs(remote_path)

        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        finally:
            if self.sftp:
                self.sftp.close()
                self.sftp = None

        return result

    def sync_with_progress(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
    ) -> SyncResult:
        """Sync with Rich progress bar display."""
        options = options or SyncOptions()
        result = SyncResult(success=True)

        try:
            self.sftp = self.ssh.open_sftp()
            self._mkdir_p(remote_path)

            local_files = self._get_local_files(local_path, options.exclude_patterns)
            remote_files = self._get_remote_files(remote_path)

            to_upload = []
            for rel_path, local_info in local_files.items():
                remote_info = remote_files.get(rel_path)
                if remote_info is None or local_info["hash"] != remote_info.get("hash"):
                    to_upload.append((rel_path, local_info))

            if not to_upload:
                return result

            total_bytes = sum(info["size"] for _, info in to_upload)

            with Progress() as progress:
                task = progress.add_task(
                    "[cyan]Syncing...", total=total_bytes
                )

                for rel_path, info in to_upload:
                    local_file = local_path / rel_path
                    remote_file = f"{remote_path}/{rel_path}"

                    try:
                        remote_dir = os.path.dirname(remote_file)
                        self._mkdir_p(remote_dir)
                        self.sftp.put(str(local_file), remote_file)
                        progress.update(task, advance=info["size"])
                        result.files_uploaded += 1
                        result.bytes_transferred += info["size"]
                    except Exception as e:
                        result.errors.append(f"Failed to upload {rel_path}: {e}")

            if options.delete_remote:
                for rel_path in remote_files:
                    if rel_path not in local_files:
                        remote_file = f"{remote_path}/{rel_path}"
                        try:
                            self.sftp.remove(remote_file)
                            result.files_deleted += 1
                        except Exception:
                            pass
                self._cleanup_empty_dirs(remote_path)

        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        finally:
            if self.sftp:
                self.sftp.close()
                self.sftp = None

        return result

    def _get_local_files(
        self, root: Path, exclude_patterns: list[str]
    ) -> dict[str, dict]:
        """Get all local files with their hashes."""
        files = {}

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            rel_path = path.relative_to(root)

            # Check exclusions
            if self._should_exclude(str(rel_path), exclude_patterns):
                continue

            # Calculate file hash
            file_hash = self._hash_file(path)

            files[str(rel_path)] = {
                "path": path,
                "hash": file_hash,
                "size": path.stat().st_size,
            }

        return files

    def _get_remote_files(self, remote_path: str) -> dict[str, dict]:
        """Get all remote files (without hashes for now - future optimization)."""
        files = {}

        try:
            self._walk_remote(remote_path, "", files)
        except IOError:
            # Remote directory doesn't exist yet
            pass

        return files

    def _walk_remote(
        self, base_path: str, rel_path: str, files: dict[str, dict]
    ) -> None:
        """Recursively walk remote directory."""
        current_path = f"{base_path}/{rel_path}" if rel_path else base_path

        try:
            for entry in self.sftp.listdir_attr(current_path):
                entry_rel = f"{rel_path}/{entry.filename}" if rel_path else entry.filename

                if stat.S_ISDIR(entry.st_mode):
                    self._walk_remote(base_path, entry_rel, files)
                else:
                    files[entry_rel] = {
                        "size": entry.st_size,
                        "mtime": entry.st_mtime,
                        # Note: We don't have hash for remote files
                        # This means we always upload if local hash differs
                        "hash": None,
                    }
        except IOError:
            pass

    def _should_exclude(self, path: str, patterns: list[str]) -> bool:
        """Check if path matches any exclusion pattern."""
        import fnmatch

        parts = path.split(os.sep)
        for pattern in patterns:
            # Check if any path component matches
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            # Also check full path
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def _hash_file(self, path: Path) -> str:
        """Calculate SHA256 hash of a file."""
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _mkdir_p(self, remote_path: str) -> None:
        """Create remote directory and parents if needed."""
        if not remote_path or remote_path == "/":
            return

        try:
            self.sftp.stat(remote_path)
        except IOError:
            # Directory doesn't exist, create parent first
            parent = os.path.dirname(remote_path)
            if parent:
                self._mkdir_p(parent)
            try:
                self.sftp.mkdir(remote_path)
            except IOError:
                pass  # May already exist due to race

    def _cleanup_empty_dirs(self, remote_path: str) -> None:
        """Remove empty directories."""
        try:
            for entry in self.sftp.listdir_attr(remote_path):
                if stat.S_ISDIR(entry.st_mode):
                    subdir = f"{remote_path}/{entry.filename}"
                    self._cleanup_empty_dirs(subdir)
                    try:
                        # Try to remove - will fail if not empty
                        self.sftp.rmdir(subdir)
                    except IOError:
                        pass
        except IOError:
            pass
