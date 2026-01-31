"""SFTP-based file synchronization using pyftpsync.

This module provides bidirectional synchronization between local project folders
and remote Pi folders over SFTP, with proper conflict detection and resolution.
"""

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from rich.progress import Progress

# Configure pyftpsync logging
logging.getLogger("ftpsync").setLevel(logging.WARNING)


# Constants
REMOTE_MANIFEST_FILENAME = ".raccoon_manifest.json"
LOCAL_CACHE_DIR = ".raccoon"
LOCAL_CACHE_FILENAME = "sync_cache.json"


class SyncDirection(Enum):
    """Direction of synchronization."""

    PUSH = "push"  # Local -> Remote
    PULL = "pull"  # Remote -> Local
    BIDIRECTIONAL = "bidirectional"  # Two-way sync


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
    files_auto_merged: int = 0  # Files successfully auto-merged
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # Only true conflicts that need manual resolution


@dataclass
class SyncOptions:
    """Options for sync operation."""

    direction: SyncDirection = SyncDirection.PUSH
    delete_remote: bool = True  # Delete files on remote not in local (PUSH)
    delete_local: bool = False  # Delete files on local not in remote (PULL)
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
            ".raccoon",  # Exclude local cache directory
        ]
    )


class RaccoonMatcher:
    """Custom matcher for pyftpsync that respects .raccoonignore patterns."""

    def __init__(self, exclude_patterns: list[str]):
        self.exclude_patterns = exclude_patterns

    def __call__(self, entry_name: str, entry_type: str) -> bool:
        """
        Determine if an entry should be included in sync.

        Args:
            entry_name: Name of the file or directory
            entry_type: 'file' or 'directory'

        Returns:
            True if entry should be included, False to exclude
        """
        # Always exclude pyftpsync metadata
        if entry_name == ".pyftpsync-meta.json":
            return False
        if entry_name == REMOTE_MANIFEST_FILENAME:
            return False

        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(entry_name, pattern):
                return False
            # Also check if it's a path pattern
            if "/" in pattern and fnmatch.fnmatch(entry_name, pattern.split("/")[-1]):
                return False
        return True


class SftpSync:
    """
    Smart file synchronization using SFTP via pyftpsync.

    Features:
    - Bidirectional sync with proper conflict detection
    - Metadata tracking for accurate change detection
    - Exclusion patterns for ignoring files
    - Optional remote/local file deletion
    - Progress reporting
    - Conflict detection with resolution support
    """

    def __init__(self, ssh_client):
        """
        Initialize the sync client.

        Args:
            ssh_client: Connected paramiko SSHClient
        """
        self.ssh = ssh_client
        self._transport = None

    def sync(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> SyncResult:
        """
        Sync files between local and remote directories.

        Args:
            local_path: Local directory to sync
            remote_path: Remote directory path on Pi
            options: Sync options including direction
            progress_callback: Called with (filename, bytes_done, bytes_total)

        Returns:
            SyncResult with statistics
        """
        options = options or SyncOptions()
        result = SyncResult(success=True)

        try:
            from ftpsync.targets import FsTarget
            from ftpsync.sftp_target import SFTPTarget
            from ftpsync.synchronizers import BiDirSynchronizer, UploadSynchronizer, DownloadSynchronizer

            # Get SSH transport info for SFTP connection
            transport = self.ssh.get_transport()
            hostname = transport.getpeername()[0]

            # Create targets
            local_target = FsTarget(str(local_path))

            # Build SFTP URL - pyftpsync needs sftp:// URL format
            # We'll pass the existing SSH transport through extra_opts
            remote_target = SFTPTarget(
                remote_path,
                hostname,
                port=22,
                username=transport.get_username(),
                password=None,  # Use key-based auth from existing connection
                timeout=30,
                extra_opts={
                    "ssh_client": self.ssh,  # Reuse existing SSH connection
                },
            )

            # Build exclude patterns
            exclude_patterns = options.exclude_patterns + [REMOTE_MANIFEST_FILENAME, ".pyftpsync-meta.json"]

            # Configure sync options
            sync_opts = {
                "verbose": 1,
                "dry_run": False,
                "match": None,  # No inclusion filter
                "exclude": ",".join(exclude_patterns),
            }

            # Select synchronizer based on direction
            if options.direction == SyncDirection.PUSH:
                sync_opts["delete"] = options.delete_remote
                sync_opts["force"] = False
                synchronizer = UploadSynchronizer(local_target, remote_target, sync_opts)

            elif options.direction == SyncDirection.PULL:
                sync_opts["delete"] = options.delete_local
                sync_opts["force"] = False
                synchronizer = DownloadSynchronizer(local_target, remote_target, sync_opts)

            else:  # BIDIRECTIONAL
                sync_opts["delete"] = False  # Don't auto-delete in bidirectional
                sync_opts["resolve"] = "newer"  # Prefer newer files, but report conflicts
                synchronizer = BiDirSynchronizer(local_target, remote_target, sync_opts)

            # Run the sync
            stats = synchronizer.run()

            # Extract stats from pyftpsync result
            result.files_uploaded = getattr(stats, 'files_written', 0) or 0
            result.files_downloaded = getattr(stats, 'download_files_written', 0) or 0
            result.files_deleted = getattr(stats, 'files_deleted', 0) or 0
            result.bytes_transferred = getattr(stats, 'bytes_written', 0) or 0

            # Check for conflicts (pyftpsync tracks these)
            conflict_files = getattr(stats, 'conflict_files', []) or []
            if conflict_files:
                result.conflicts = list(conflict_files)

        except ImportError as e:
            result.success = False
            result.errors.append(f"pyftpsync not installed: {e}")
        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        return result

    def sync_with_progress(
        self,
        local_path: Path,
        remote_path: str,
        options: Optional[SyncOptions] = None,
    ) -> SyncResult:
        """
        Sync with Rich progress bar display.

        Args:
            local_path: Local directory to sync
            remote_path: Remote directory path on Pi
            options: Sync options including direction

        Returns:
            SyncResult with statistics
        """
        options = options or SyncOptions()
        result = SyncResult(success=True)

        try:
            # Use the simpler paramiko-based sync for now with progress
            # pyftpsync's progress callbacks are complex to integrate with Rich
            result = self._sync_with_paramiko(local_path, remote_path, options)

        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        return result

    def _sync_with_paramiko(
        self,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
    ) -> SyncResult:
        """
        Perform sync using paramiko directly with proper bidirectional support.

        This is a refined implementation that properly handles:
        - Local -> Remote changes (uploads)
        - Remote -> Local changes (downloads)
        - Conflict detection when both sides changed
        - Auto-merge for compatible text changes
        """
        import hashlib
        import json
        import posixpath
        import stat

        result = SyncResult(success=True)
        sftp = None

        try:
            sftp = self.ssh.open_sftp()

            # Ensure remote directory exists
            self._mkdir_p(sftp, remote_path)

            # Load/create sync manifest
            manifest = self._load_manifest(sftp, remote_path)

            # Build exclude patterns
            exclude_patterns = options.exclude_patterns + [".pyftpsync-meta.json", REMOTE_MANIFEST_FILENAME]

            # Get file listings
            local_files = self._get_local_files(local_path, exclude_patterns)
            remote_files = self._get_remote_files(sftp, remote_path, exclude_patterns)

            all_paths = set(local_files.keys()) | set(remote_files.keys())

            to_upload = []
            to_download = []
            conflicts = []

            for rel_path in all_paths:
                local_info = local_files.get(rel_path)
                remote_info = remote_files.get(rel_path)
                manifest_entry = manifest.get(rel_path, {})

                if options.direction == SyncDirection.PUSH:
                    if local_info:
                        manifest_hash = manifest_entry.get("hash")
                        if not manifest_hash or local_info["hash"] != manifest_hash:
                            to_upload.append((rel_path, local_info))
                    elif options.delete_remote and remote_info:
                        # File exists on remote but not locally - delete
                        self._delete_remote_file(sftp, remote_path, rel_path, result)

                elif options.direction == SyncDirection.PULL:
                    if remote_info:
                        if not local_info:
                            to_download.append((rel_path, remote_info))
                        elif manifest_entry:
                            manifest_hash = manifest_entry.get("hash")
                            if manifest_hash and local_info["hash"] == manifest_hash:
                                # Local unchanged since last sync - check if remote changed
                                if remote_info.get("mtime", 0) > manifest_entry.get("mtime", 0):
                                    to_download.append((rel_path, remote_info))
                    elif options.delete_local and local_info:
                        # File exists locally but not on remote - delete
                        self._delete_local_file(local_path, rel_path, result)

                else:  # BIDIRECTIONAL
                    was_previously_synced = rel_path in manifest

                    if local_info and not remote_info:
                        if was_previously_synced:
                            # Was synced before, now missing on remote = deleted remotely
                            # Delete locally to propagate the deletion
                            if options.delete_local:
                                self._delete_local_file(local_path, rel_path, result)
                                manifest.pop(rel_path, None)
                        else:
                            # New local file, upload it
                            to_upload.append((rel_path, local_info))
                    elif remote_info and not local_info:
                        if was_previously_synced:
                            # Was synced before, now missing locally = deleted locally
                            # Delete on remote to propagate the deletion
                            if options.delete_remote:
                                self._delete_remote_file(sftp, remote_path, rel_path, result)
                                manifest.pop(rel_path, None)
                        else:
                            # New remote file, download it
                            to_download.append((rel_path, remote_info))
                    elif local_info and remote_info:
                        # Both have it - check for changes
                        manifest_hash = manifest_entry.get("hash")
                        manifest_mtime = manifest_entry.get("mtime", 0)

                        local_changed = not manifest_hash or local_info["hash"] != manifest_hash
                        remote_changed = remote_info.get("mtime", 0) > manifest_mtime + 1  # 1s tolerance

                        if local_changed and remote_changed:
                            # Both changed - try auto-merge
                            merge_result = self._try_auto_merge(
                                sftp, local_path, remote_path, rel_path, local_info, remote_info, result
                            )
                            if merge_result == "conflict":
                                conflicts.append(rel_path)
                            elif merge_result == "uploaded":
                                result.files_uploaded += 1
                                result.files_auto_merged += 1
                        elif local_changed:
                            to_upload.append((rel_path, local_info))
                        elif remote_changed:
                            to_download.append((rel_path, remote_info))

            # Perform transfers with progress
            total_bytes = (
                sum(info["size"] for _, info in to_upload)
                + sum(info.get("size", 0) for _, info in to_download)
            )

            if to_upload or to_download:
                with Progress() as progress:
                    task = progress.add_task("[cyan]Syncing...", total=total_bytes or 1)

                    # Upload files
                    for rel_path, info in to_upload:
                        local_file = local_path / rel_path
                        remote_file = f"{remote_path}/{rel_path}"

                        try:
                            remote_dir = posixpath.dirname(remote_file)
                            self._mkdir_p(sftp, remote_dir)
                            sftp.put(str(local_file), remote_file)
                            progress.update(task, advance=info["size"])

                            # Update manifest
                            remote_stat = sftp.stat(remote_file)
                            manifest[rel_path] = {
                                "hash": info["hash"],
                                "mtime": remote_stat.st_mtime,
                                "size": info["size"],
                            }

                            result.files_uploaded += 1
                            result.bytes_transferred += info["size"]
                        except Exception as e:
                            result.errors.append(f"Failed to upload {rel_path}: {e}")

                    # Download files
                    for rel_path, info in to_download:
                        local_file = local_path / rel_path
                        remote_file = f"{remote_path}/{rel_path}"

                        try:
                            local_file.parent.mkdir(parents=True, exist_ok=True)
                            sftp.get(remote_file, str(local_file))
                            progress.update(task, advance=info.get("size", 0))

                            # Update manifest with local hash
                            new_hash = self._compute_hash(local_file)
                            manifest[rel_path] = {
                                "hash": new_hash,
                                "mtime": info.get("mtime", 0),
                                "size": info.get("size", 0),
                            }

                            result.files_downloaded += 1
                            result.bytes_transferred += info.get("size", 0)
                        except Exception as e:
                            result.errors.append(f"Failed to download {rel_path}: {e}")

            # Save manifest
            self._save_manifest(sftp, remote_path, manifest)

            # Record conflicts
            result.conflicts = conflicts

        except Exception as e:
            result.success = False
            result.errors.append(str(e))
        finally:
            if sftp:
                sftp.close()

        return result

    def _try_auto_merge(
        self,
        sftp,
        local_path: Path,
        remote_path: str,
        rel_path: str,
        local_info: dict,
        remote_info: dict,
        result: SyncResult,
    ) -> str:
        """
        Try to auto-merge changes when both sides modified a file.

        Returns:
            "uploaded" if merged and uploaded
            "conflict" if couldn't merge
            "identical" if files are actually the same
        """
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus
        import posixpath

        local_file = local_path / rel_path
        remote_file = f"{remote_path}/{rel_path}"

        try:
            # Read both versions
            local_content = local_file.read_bytes()
            with sftp.open(remote_file, "rb") as f:
                remote_content = f.read()

            # Attempt merge
            merge_result = attempt_auto_merge(local_content, remote_content, rel_path)

            if merge_result.status == MergeStatus.SUCCESS:
                # Write merged content locally and upload
                local_file.write_bytes(merge_result.merged_content)
                sftp.put(str(local_file), remote_file)
                result.bytes_transferred += len(merge_result.merged_content)
                return "uploaded"

            elif merge_result.status == MergeStatus.IDENTICAL:
                # Files are the same after normalization
                return "identical"

            else:
                # CONFLICT or BINARY - can't auto-merge
                return "conflict"

        except Exception:
            return "conflict"

    def _load_manifest(self, sftp, remote_path: str) -> dict:
        """Load sync manifest from remote."""
        import json
        import posixpath

        manifest_path = posixpath.join(remote_path, ".raccoon_manifest.json")
        try:
            with sftp.open(manifest_path, "r") as f:
                return json.load(f)
        except:
            return {}

    def _save_manifest(self, sftp, remote_path: str, manifest: dict) -> None:
        """Save sync manifest to remote."""
        import json
        import posixpath

        manifest_path = posixpath.join(remote_path, ".raccoon_manifest.json")
        try:
            with sftp.open(manifest_path, "w") as f:
                f.write(json.dumps(manifest, indent=2))
        except:
            pass

    def _get_local_files(self, root: Path, exclude_patterns: list[str]) -> dict[str, dict]:
        """Get all local files with their hashes."""
        files = {}

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            rel_path = path.relative_to(root).as_posix()

            if self._should_exclude(rel_path, exclude_patterns):
                continue

            file_stat = path.stat()
            files[rel_path] = {
                "hash": self._compute_hash(path),
                "size": file_stat.st_size,
                "mtime": file_stat.st_mtime,
            }

        return files

    def _get_remote_files(self, sftp, remote_path: str, exclude_patterns: list[str]) -> dict[str, dict]:
        """Get all remote files with metadata."""
        import stat

        files = {}

        def walk(base: str, rel: str):
            current = f"{base}/{rel}" if rel else base
            try:
                for entry in sftp.listdir_attr(current):
                    entry_rel = f"{rel}/{entry.filename}" if rel else entry.filename

                    if self._should_exclude(entry_rel, exclude_patterns):
                        continue

                    if stat.S_ISDIR(entry.st_mode):
                        walk(base, entry_rel)
                    else:
                        files[entry_rel] = {
                            "size": entry.st_size,
                            "mtime": entry.st_mtime,
                        }
            except IOError:
                pass

        walk(remote_path, "")
        return files

    def _should_exclude(self, path: str, patterns: list[str]) -> bool:
        """Check if path matches any exclusion pattern."""
        parts = path.split("/")
        for pattern in patterns:
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def _compute_hash(self, path: Path) -> str:
        """Compute SHA256 hash of a file with line ending normalization for text."""
        import hashlib

        TEXT_EXTENSIONS = {
            '.py', '.yml', '.yaml', '.json', '.txt', '.md', '.rst',
            '.cfg', '.ini', '.toml', '.sh', '.bash', '.zsh',
            '.html', '.css', '.js', '.ts', '.jsx', '.tsx',
            '.xml', '.csv', '.env', '.gitignore', '.dockerignore',
        }

        hasher = hashlib.sha256()
        try:
            is_text = path.suffix.lower() in TEXT_EXTENSIONS

            if is_text:
                content = path.read_bytes()
                content = content.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
                hasher.update(content)
            else:
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        hasher.update(chunk)
        except IOError:
            return ""

        return hasher.hexdigest()

    def _mkdir_p(self, sftp, remote_path: str) -> None:
        """Create remote directory and parents if needed."""
        import posixpath

        if not remote_path or remote_path == "/":
            return

        try:
            sftp.stat(remote_path)
        except IOError:
            parent = posixpath.dirname(remote_path)
            if parent:
                self._mkdir_p(sftp, parent)
            try:
                sftp.mkdir(remote_path)
            except IOError:
                pass

    def _delete_remote_file(self, sftp, remote_path: str, rel_path: str, result: SyncResult) -> None:
        """Delete a file from remote."""
        try:
            sftp.remove(f"{remote_path}/{rel_path}")
            result.files_deleted += 1
        except:
            pass

    def _delete_local_file(self, local_path: Path, rel_path: str, result: SyncResult) -> None:
        """Delete a file locally."""
        try:
            (local_path / rel_path).unlink()
            result.files_deleted += 1
        except:
            pass


# Keep these for backward compatibility with existing code
class HashCache:
    """
    Local hash cache with mtime-based invalidation.
    Kept for backward compatibility with tests and other modules.
    """

    TEXT_EXTENSIONS = {
        '.py', '.yml', '.yaml', '.json', '.txt', '.md', '.rst',
        '.cfg', '.ini', '.toml', '.sh', '.bash', '.zsh',
        '.html', '.css', '.js', '.ts', '.jsx', '.tsx',
        '.xml', '.csv', '.env', '.gitignore', '.dockerignore',
    }

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.cache_dir = project_root / LOCAL_CACHE_DIR
        self.cache_file = self.cache_dir / LOCAL_CACHE_FILENAME
        self._cache: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        import json
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    self._cache = json.load(f)
            except:
                self._cache = {}

    def save_cache(self) -> None:
        import json
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(self._cache, f, indent=2)
        except:
            pass

    def get_hash(self, rel_path: str, file_path: Path) -> str:
        import hashlib

        try:
            file_stat = file_path.stat()
            current_mtime = file_stat.st_mtime
            current_size = file_stat.st_size
        except OSError:
            return self._compute_hash(file_path)

        cached = self._cache.get(rel_path)
        if cached and cached.get("mtime") == current_mtime and cached.get("size") == current_size:
            return cached["hash"]

        file_hash = self._compute_hash(file_path)
        self._cache[rel_path] = {
            "hash": file_hash,
            "mtime": current_mtime,
            "size": current_size,
        }
        return file_hash

    def _compute_hash(self, file_path: Path) -> str:
        import hashlib

        hasher = hashlib.sha256()
        try:
            is_text = file_path.suffix.lower() in self.TEXT_EXTENSIONS

            if is_text:
                with open(file_path, "rb") as f:
                    content = f.read()
                content = content.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
                hasher.update(content)
            else:
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        hasher.update(chunk)
        except IOError:
            return ""
        return hasher.hexdigest()

    def compute_hash_from_bytes(self, content: bytes, rel_path: str) -> str:
        import hashlib

        suffix = Path(rel_path).suffix.lower()
        is_text = suffix in self.TEXT_EXTENSIONS

        if is_text:
            content = content.replace(b'\r\n', b'\n').replace(b'\r', b'\n')

        return hashlib.sha256(content).hexdigest()

    def invalidate(self, rel_path: str) -> None:
        self._cache.pop(rel_path, None)

    def clear(self) -> None:
        self._cache.clear()


class RemoteManifest:
    """
    Manages the remote manifest file.
    Kept for backward compatibility.
    """

    def __init__(self, sftp, remote_path: str):
        import posixpath
        self.sftp = sftp
        self.remote_path = remote_path
        self.manifest_path = posixpath.join(remote_path, ".raccoon_manifest.json")
        self._manifest: dict[str, dict] = {}
        self._dirty = False

    def load(self) -> None:
        import json
        try:
            with self.sftp.open(self.manifest_path, "r") as f:
                self._manifest = json.load(f)
        except:
            self._manifest = {}

    def save(self) -> None:
        import json
        if not self._dirty:
            return
        try:
            with self.sftp.open(self.manifest_path, "w") as f:
                f.write(json.dumps(self._manifest, indent=2))
            self._dirty = False
        except:
            pass

    def get(self, rel_path: str) -> Optional[dict]:
        return self._manifest.get(rel_path)

    def set(self, rel_path: str, file_hash: str, mtime: float, size: int) -> None:
        self._manifest[rel_path] = {
            "hash": file_hash,
            "mtime": mtime,
            "size": size,
        }
        self._dirty = True

    def remove(self, rel_path: str) -> None:
        if rel_path in self._manifest:
            del self._manifest[rel_path]
            self._dirty = True

    def get_all_files(self) -> dict[str, dict]:
        return self._manifest.copy()

    def clear(self) -> None:
        self._manifest.clear()
        self._dirty = True
