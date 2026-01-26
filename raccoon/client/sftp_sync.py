"""SFTP-based file synchronization using paramiko."""

import hashlib
import json
import posixpath
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import paramiko
from rich.progress import Progress, TaskID


# Constants
REMOTE_MANIFEST_FILENAME = ".raccoon_manifest.json"
LOCAL_CACHE_DIR = ".raccoon"
LOCAL_CACHE_FILENAME = "sync_cache.json"


class SyncDirection(Enum):
    """Direction of synchronization."""

    PUSH = "push"  # Local -> Remote
    PULL = "pull"  # Remote -> Local
    BIDIRECTIONAL = "bidirectional"  # Two-way sync based on mtime


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


class HashCache:
    """
    Local hash cache with mtime-based invalidation.

    Stores computed file hashes locally to avoid re-hashing unchanged files.
    Cache entries are invalidated when file mtime changes.
    """

    def __init__(self, project_root: Path):
        """
        Initialize the hash cache.

        Args:
            project_root: Path to the project root directory
        """
        self.project_root = project_root
        self.cache_dir = project_root / LOCAL_CACHE_DIR
        self.cache_file = self.cache_dir / LOCAL_CACHE_FILENAME
        self._cache: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load cache from disk if it exists."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._cache = {}

    def save_cache(self) -> None:
        """Save cache to disk."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(self._cache, f, indent=2)
        except IOError:
            pass  # Cache save failure is non-fatal

    def get_hash(self, rel_path: str, file_path: Path) -> str:
        """
        Get hash for a file, using cache if mtime matches.

        Args:
            rel_path: Relative path (used as cache key)
            file_path: Absolute path to the file

        Returns:
            SHA256 hash of the file
        """
        try:
            file_stat = file_path.stat()
            current_mtime = file_stat.st_mtime
            current_size = file_stat.st_size
        except OSError:
            # File doesn't exist or can't be accessed, compute hash directly
            return self._compute_hash(file_path)

        cached = self._cache.get(rel_path)
        if cached and cached.get("mtime") == current_mtime and cached.get("size") == current_size:
            return cached["hash"]

        # Cache miss or invalidated - compute fresh hash
        file_hash = self._compute_hash(file_path)
        self._cache[rel_path] = {
            "hash": file_hash,
            "mtime": current_mtime,
            "size": current_size,
        }
        return file_hash

    # Text file extensions for line ending normalization
    TEXT_EXTENSIONS = {
        '.py', '.yml', '.yaml', '.json', '.txt', '.md', '.rst',
        '.cfg', '.ini', '.toml', '.sh', '.bash', '.zsh',
        '.html', '.css', '.js', '.ts', '.jsx', '.tsx',
        '.xml', '.csv', '.env', '.gitignore', '.dockerignore',
    }

    def _compute_hash(self, file_path: Path, normalize_line_endings: bool = True) -> str:
        """
        Compute SHA256 hash of a file.

        For text files, normalizes line endings (CRLF -> LF, CR -> LF) to ensure
        consistent hashes across Windows/Linux platforms.

        Args:
            file_path: Path to the file to hash
            normalize_line_endings: Whether to normalize line endings for text files

        Returns:
            SHA256 hash as hex string, or empty string on error
        """
        hasher = hashlib.sha256()
        try:
            # Check if it's a text file by extension
            is_text = file_path.suffix.lower() in self.TEXT_EXTENSIONS

            if is_text and normalize_line_endings:
                with open(file_path, "rb") as f:
                    content = f.read()
                # Normalize CRLF -> LF and CR -> LF
                content = content.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
                hasher.update(content)
            else:
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        hasher.update(chunk)
        except IOError:
            return ""
        return hasher.hexdigest()

    def invalidate(self, rel_path: str) -> None:
        """Remove a file from the cache."""
        self._cache.pop(rel_path, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()


class RemoteManifest:
    """
    Manages the remote manifest file for tracking synced file hashes.

    The manifest stores {filename: {hash, mtime, size}} for all synced files,
    allowing hash comparison without re-downloading files.
    """

    def __init__(self, sftp: paramiko.SFTPClient, remote_path: str):
        """
        Initialize the remote manifest manager.

        Args:
            sftp: Active SFTP client
            remote_path: Base remote path for the project
        """
        self.sftp = sftp
        self.remote_path = remote_path
        self.manifest_path = posixpath.join(remote_path, REMOTE_MANIFEST_FILENAME)
        self._manifest: dict[str, dict] = {}
        self._dirty = False

    def load(self) -> None:
        """Load manifest from remote if it exists."""
        try:
            with self.sftp.open(self.manifest_path, "r") as f:
                self._manifest = json.load(f)
        except (IOError, json.JSONDecodeError):
            self._manifest = {}

    def save(self) -> None:
        """Save manifest to remote."""
        if not self._dirty:
            return
        try:
            with self.sftp.open(self.manifest_path, "w") as f:
                f.write(json.dumps(self._manifest, indent=2))
            self._dirty = False
        except IOError:
            pass  # Manifest save failure is non-fatal

    def get(self, rel_path: str) -> Optional[dict]:
        """
        Get manifest entry for a file.

        Args:
            rel_path: Relative path of the file

        Returns:
            Dict with hash, mtime, size or None if not found
        """
        return self._manifest.get(rel_path)

    def set(self, rel_path: str, file_hash: str, mtime: float, size: int) -> None:
        """
        Set manifest entry for a file.

        Args:
            rel_path: Relative path of the file
            file_hash: SHA256 hash of the file
            mtime: Modification time
            size: File size in bytes
        """
        self._manifest[rel_path] = {
            "hash": file_hash,
            "mtime": mtime,
            "size": size,
        }
        self._dirty = True

    def remove(self, rel_path: str) -> None:
        """Remove a file from the manifest."""
        if rel_path in self._manifest:
            del self._manifest[rel_path]
            self._dirty = True

    def get_all_files(self) -> dict[str, dict]:
        """Get all files in the manifest."""
        return self._manifest.copy()

    def clear(self) -> None:
        """Clear all manifest entries."""
        self._manifest.clear()
        self._dirty = True


class SftpSync:
    """
    Smart file synchronization using SFTP.

    Features:
    - Hash-based change detection (only upload changed files)
    - Local hash caching with mtime-based invalidation
    - Remote manifest for tracking synced file hashes
    - Bidirectional sync support (PUSH, PULL, BIDIRECTIONAL)
    - Exclusion patterns for ignoring files
    - Optional remote/local file deletion
    - Progress reporting
    - Conflict detection in bidirectional mode
    """

    def __init__(self, ssh_client: paramiko.SSHClient):
        """
        Initialize the sync client.

        Args:
            ssh_client: Connected paramiko SSHClient
        """
        self.ssh = ssh_client
        self.sftp: Optional[paramiko.SFTPClient] = None
        self._hash_cache: Optional[HashCache] = None
        self._remote_manifest: Optional[RemoteManifest] = None

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
            self.sftp = self.ssh.open_sftp()
            self._hash_cache = HashCache(local_path)
            self._remote_manifest = RemoteManifest(self.sftp, remote_path)

            # Ensure remote directory exists
            self._mkdir_p(remote_path)

            # Load remote manifest
            self._remote_manifest.load()

            # Build exclude patterns including manifest file
            exclude_patterns = options.exclude_patterns + [REMOTE_MANIFEST_FILENAME]

            if options.direction == SyncDirection.PUSH:
                self._sync_push(local_path, remote_path, options, exclude_patterns, result, progress_callback)
            elif options.direction == SyncDirection.PULL:
                self._sync_pull(local_path, remote_path, options, exclude_patterns, result, progress_callback)
            elif options.direction == SyncDirection.BIDIRECTIONAL:
                self._sync_bidirectional(local_path, remote_path, options, exclude_patterns, result, progress_callback)

            # Save caches
            self._hash_cache.save_cache()
            self._remote_manifest.save()

        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        finally:
            if self.sftp:
                self.sftp.close()
                self.sftp = None

        return result

    def _sync_push(
        self,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
        exclude_patterns: list[str],
        result: SyncResult,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """
        Push local files to remote.

        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            options: Sync options
            exclude_patterns: Patterns to exclude
            result: SyncResult to update
            progress_callback: Progress callback function
        """
        # Get local files with hashes
        local_files = self._get_local_files(local_path, exclude_patterns)

        # Get remote files from manifest
        remote_manifest_files = self._remote_manifest.get_all_files()

        # Calculate what to upload
        to_upload = []
        for rel_path, local_info in local_files.items():
            manifest_entry = remote_manifest_files.get(rel_path)
            if manifest_entry is None or local_info["hash"] != manifest_entry.get("hash"):
                to_upload.append((rel_path, local_info))

        # Upload files
        for rel_path, info in to_upload:
            local_file = local_path / rel_path
            remote_file = f"{remote_path}/{rel_path}"

            try:
                remote_dir = posixpath.dirname(remote_file)
                self._mkdir_p(remote_dir)

                if progress_callback:
                    progress_callback(rel_path, 0, info["size"])

                self.sftp.put(str(local_file), remote_file)

                if progress_callback:
                    progress_callback(rel_path, info["size"], info["size"])

                # Update remote manifest
                file_stat = local_file.stat()
                self._remote_manifest.set(rel_path, info["hash"], file_stat.st_mtime, info["size"])

                result.files_uploaded += 1
                result.bytes_transferred += info["size"]
            except Exception as e:
                result.errors.append(f"Failed to upload {rel_path}: {e}")

        # Delete remote files not in local
        if options.delete_remote:
            # Get actual remote files (not just manifest)
            remote_files = self._get_remote_files(remote_path, exclude_patterns)
            for rel_path in remote_files:
                if rel_path not in local_files:
                    remote_file = f"{remote_path}/{rel_path}"
                    try:
                        self.sftp.remove(remote_file)
                        self._remote_manifest.remove(rel_path)
                        result.files_deleted += 1
                    except Exception as e:
                        result.errors.append(f"Failed to delete {rel_path}: {e}")

            self._cleanup_empty_dirs(remote_path)

    def _sync_pull(
        self,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
        exclude_patterns: list[str],
        result: SyncResult,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """
        Pull remote files to local.

        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            options: Sync options
            exclude_patterns: Patterns to exclude
            result: SyncResult to update
            progress_callback: Progress callback function
        """
        # Get remote files
        remote_files = self._get_remote_files(remote_path, exclude_patterns)

        # Get local files
        local_files = self._get_local_files(local_path, exclude_patterns)

        # Get remote manifest for hash comparison
        remote_manifest_files = self._remote_manifest.get_all_files()

        # Calculate what to download
        to_download = []
        for rel_path, remote_info in remote_files.items():
            local_info = local_files.get(rel_path)
            manifest_entry = remote_manifest_files.get(rel_path)

            if local_info is None:
                # File doesn't exist locally
                to_download.append((rel_path, remote_info))
            elif manifest_entry and local_info["hash"] != manifest_entry.get("hash"):
                # Local hash differs from manifest hash
                to_download.append((rel_path, remote_info))
            elif not manifest_entry:
                # No manifest entry, compare by mtime/size
                if remote_info.get("mtime", 0) > local_info.get("mtime", 0):
                    to_download.append((rel_path, remote_info))

        # Download files
        for rel_path, info in to_download:
            local_file = local_path / rel_path
            remote_file = f"{remote_path}/{rel_path}"

            try:
                local_file.parent.mkdir(parents=True, exist_ok=True)

                if progress_callback:
                    progress_callback(rel_path, 0, info.get("size", 0))

                self.sftp.get(remote_file, str(local_file))

                if progress_callback:
                    progress_callback(rel_path, info.get("size", 0), info.get("size", 0))

                # Invalidate local cache for this file
                self._hash_cache.invalidate(rel_path)

                result.files_downloaded += 1
                result.bytes_transferred += info.get("size", 0)
            except Exception as e:
                result.errors.append(f"Failed to download {rel_path}: {e}")

        # Delete local files not in remote
        if options.delete_local:
            for rel_path in local_files:
                if rel_path not in remote_files:
                    local_file = local_path / rel_path
                    try:
                        local_file.unlink()
                        self._hash_cache.invalidate(rel_path)
                        result.files_deleted += 1
                    except Exception as e:
                        result.errors.append(f"Failed to delete {rel_path}: {e}")

            self._cleanup_empty_local_dirs(local_path)

    def _sync_bidirectional(
        self,
        local_path: Path,
        remote_path: str,
        options: SyncOptions,
        exclude_patterns: list[str],
        result: SyncResult,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """
        Bidirectional sync using mtime to determine direction.

        Attempts auto-merge when both local and remote have changed.
        Only reports conflicts when auto-merge fails.

        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            options: Sync options
            exclude_patterns: Patterns to exclude
            result: SyncResult to update
            progress_callback: Progress callback function
        """
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        # Get local and remote files
        local_files = self._get_local_files(local_path, exclude_patterns)
        remote_files = self._get_remote_files(remote_path, exclude_patterns)
        remote_manifest_files = self._remote_manifest.get_all_files()

        # All unique paths
        all_paths = set(local_files.keys()) | set(remote_files.keys())

        to_upload = []
        to_download = []
        to_merge = []  # (rel_path, local_info, remote_info) for files needing merge

        for rel_path in all_paths:
            local_info = local_files.get(rel_path)
            remote_info = remote_files.get(rel_path)
            manifest_entry = remote_manifest_files.get(rel_path)

            if local_info and not remote_info:
                # Only local has the file - upload it
                to_upload.append((rel_path, local_info))
            elif remote_info and not local_info:
                # Only remote has the file - download it
                to_download.append((rel_path, remote_info))
            elif local_info and remote_info:
                # Both have the file - compare
                local_hash = local_info["hash"]
                manifest_hash = manifest_entry.get("hash") if manifest_entry else None

                if manifest_hash:
                    local_changed = local_hash != manifest_hash
                    # For remote, check if mtime or size changed since manifest
                    remote_changed = (
                        remote_info.get("mtime", 0) != manifest_entry.get("mtime", 0)
                        or remote_info.get("size", 0) != manifest_entry.get("size", 0)
                    )

                    if local_changed and remote_changed:
                        # Both changed - try to auto-merge
                        to_merge.append((rel_path, local_info, remote_info))
                    elif local_changed:
                        # Only local changed - upload
                        to_upload.append((rel_path, local_info))
                    elif remote_changed:
                        # Only remote changed - download
                        to_download.append((rel_path, remote_info))
                    # else: neither changed, skip
                else:
                    # No manifest entry - use mtime to decide
                    local_mtime = local_info.get("mtime", 0)
                    remote_mtime = remote_info.get("mtime", 0)

                    if local_mtime > remote_mtime:
                        to_upload.append((rel_path, local_info))
                    elif remote_mtime > local_mtime:
                        to_download.append((rel_path, remote_info))
                    # else: same mtime, skip

        # Attempt auto-merge for files where both sides changed
        for rel_path, local_info, remote_info in to_merge:
            local_file = local_path / rel_path
            remote_file = f"{remote_path}/{rel_path}"

            try:
                # Read both versions
                local_content = local_file.read_bytes()
                with self.sftp.open(remote_file, "rb") as f:
                    remote_content = f.read()

                # Attempt auto-merge
                merge_result = attempt_auto_merge(local_content, remote_content, rel_path)

                if merge_result.status == MergeStatus.SUCCESS:
                    # Auto-merge succeeded - write merged content locally and upload
                    local_file.write_bytes(merge_result.merged_content)

                    # Upload merged version to remote
                    remote_dir = posixpath.dirname(remote_file)
                    self._mkdir_p(remote_dir)
                    self.sftp.put(str(local_file), remote_file)

                    # Update caches
                    self._hash_cache.invalidate(rel_path)
                    new_hash = self._hash_cache.get_hash(rel_path, local_file)
                    file_stat = local_file.stat()
                    self._remote_manifest.set(rel_path, new_hash, file_stat.st_mtime, file_stat.st_size)

                    result.files_auto_merged += 1
                    result.bytes_transferred += len(merge_result.merged_content)

                elif merge_result.status == MergeStatus.IDENTICAL:
                    # Files are identical after normalization - just update manifest
                    file_stat = local_file.stat()
                    self._remote_manifest.set(rel_path, local_info["hash"], file_stat.st_mtime, file_stat.st_size)

                else:
                    # CONFLICT or BINARY - cannot auto-merge
                    result.conflicts.append(rel_path)

            except Exception as e:
                result.errors.append(f"Failed to merge {rel_path}: {e}")
                result.conflicts.append(rel_path)

        # Perform uploads
        for rel_path, info in to_upload:
            local_file = local_path / rel_path
            remote_file = f"{remote_path}/{rel_path}"

            try:
                remote_dir = posixpath.dirname(remote_file)
                self._mkdir_p(remote_dir)

                if progress_callback:
                    progress_callback(rel_path, 0, info["size"])

                self.sftp.put(str(local_file), remote_file)

                if progress_callback:
                    progress_callback(rel_path, info["size"], info["size"])

                file_stat = local_file.stat()
                self._remote_manifest.set(rel_path, info["hash"], file_stat.st_mtime, info["size"])

                result.files_uploaded += 1
                result.bytes_transferred += info["size"]
            except Exception as e:
                result.errors.append(f"Failed to upload {rel_path}: {e}")

        # Perform downloads
        for rel_path, info in to_download:
            local_file = local_path / rel_path
            remote_file = f"{remote_path}/{rel_path}"

            try:
                local_file.parent.mkdir(parents=True, exist_ok=True)

                if progress_callback:
                    progress_callback(rel_path, 0, info.get("size", 0))

                self.sftp.get(remote_file, str(local_file))

                if progress_callback:
                    progress_callback(rel_path, info.get("size", 0), info.get("size", 0))

                # Re-hash the downloaded file and update manifest
                new_hash = self._hash_cache.get_hash(rel_path, local_file)
                file_stat = local_file.stat()
                self._remote_manifest.set(rel_path, new_hash, file_stat.st_mtime, file_stat.st_size)

                result.files_downloaded += 1
                result.bytes_transferred += info.get("size", 0)
            except Exception as e:
                result.errors.append(f"Failed to download {rel_path}: {e}")

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
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        options = options or SyncOptions()
        result = SyncResult(success=True)

        try:
            self.sftp = self.ssh.open_sftp()
            self._hash_cache = HashCache(local_path)
            self._remote_manifest = RemoteManifest(self.sftp, remote_path)

            self._mkdir_p(remote_path)
            self._remote_manifest.load()

            exclude_patterns = options.exclude_patterns + [REMOTE_MANIFEST_FILENAME]

            # Get file lists based on direction
            local_files = self._get_local_files(local_path, exclude_patterns)
            remote_files = self._get_remote_files(remote_path, exclude_patterns)
            remote_manifest_files = self._remote_manifest.get_all_files()

            to_upload = []
            to_download = []
            to_merge = []  # (rel_path, local_info, remote_info) for files needing merge

            if options.direction == SyncDirection.PUSH:
                for rel_path, local_info in local_files.items():
                    manifest_entry = remote_manifest_files.get(rel_path)
                    if manifest_entry is None or local_info["hash"] != manifest_entry.get("hash"):
                        to_upload.append((rel_path, local_info))

            elif options.direction == SyncDirection.PULL:
                for rel_path, remote_info in remote_files.items():
                    local_info = local_files.get(rel_path)
                    manifest_entry = remote_manifest_files.get(rel_path)
                    if local_info is None:
                        to_download.append((rel_path, remote_info))
                    elif manifest_entry and local_info["hash"] != manifest_entry.get("hash"):
                        to_download.append((rel_path, remote_info))
                    elif not manifest_entry and remote_info.get("mtime", 0) > local_info.get("mtime", 0):
                        to_download.append((rel_path, remote_info))

            elif options.direction == SyncDirection.BIDIRECTIONAL:
                all_paths = set(local_files.keys()) | set(remote_files.keys())
                for rel_path in all_paths:
                    local_info = local_files.get(rel_path)
                    remote_info = remote_files.get(rel_path)
                    manifest_entry = remote_manifest_files.get(rel_path)

                    if local_info and not remote_info:
                        to_upload.append((rel_path, local_info))
                    elif remote_info and not local_info:
                        to_download.append((rel_path, remote_info))
                    elif local_info and remote_info:
                        local_hash = local_info["hash"]
                        manifest_hash = manifest_entry.get("hash") if manifest_entry else None

                        if manifest_hash:
                            local_changed = local_hash != manifest_hash
                            remote_changed = (
                                remote_info.get("mtime", 0) != manifest_entry.get("mtime", 0)
                                or remote_info.get("size", 0) != manifest_entry.get("size", 0)
                            )
                            if local_changed and remote_changed:
                                # Both changed - try to auto-merge
                                to_merge.append((rel_path, local_info, remote_info))
                            elif local_changed:
                                to_upload.append((rel_path, local_info))
                            elif remote_changed:
                                to_download.append((rel_path, remote_info))
                        else:
                            local_mtime = local_info.get("mtime", 0)
                            remote_mtime = remote_info.get("mtime", 0)
                            if local_mtime > remote_mtime:
                                to_upload.append((rel_path, local_info))
                            elif remote_mtime > local_mtime:
                                to_download.append((rel_path, remote_info))

            # Handle auto-merge first (before progress bar to avoid UI issues)
            for rel_path, local_info, remote_info in to_merge:
                local_file = local_path / rel_path
                remote_file = f"{remote_path}/{rel_path}"

                try:
                    # Read both versions
                    local_content = local_file.read_bytes()
                    with self.sftp.open(remote_file, "rb") as f:
                        remote_content = f.read()

                    # Attempt auto-merge
                    merge_result = attempt_auto_merge(local_content, remote_content, rel_path)

                    if merge_result.status == MergeStatus.SUCCESS:
                        # Auto-merge succeeded - write merged content locally and upload
                        local_file.write_bytes(merge_result.merged_content)

                        # Upload merged version to remote
                        remote_dir = posixpath.dirname(remote_file)
                        self._mkdir_p(remote_dir)
                        self.sftp.put(str(local_file), remote_file)

                        # Update caches
                        self._hash_cache.invalidate(rel_path)
                        new_hash = self._hash_cache.get_hash(rel_path, local_file)
                        file_stat = local_file.stat()
                        self._remote_manifest.set(rel_path, new_hash, file_stat.st_mtime, file_stat.st_size)

                        result.files_auto_merged += 1
                        result.bytes_transferred += len(merge_result.merged_content)

                    elif merge_result.status == MergeStatus.IDENTICAL:
                        # Files are identical after normalization - just update manifest
                        file_stat = local_file.stat()
                        self._remote_manifest.set(rel_path, local_info["hash"], file_stat.st_mtime, file_stat.st_size)

                    else:
                        # CONFLICT or BINARY - cannot auto-merge
                        result.conflicts.append(rel_path)

                except Exception as e:
                    result.errors.append(f"Failed to merge {rel_path}: {e}")
                    result.conflicts.append(rel_path)

            if not to_upload and not to_download:
                self._hash_cache.save_cache()
                self._remote_manifest.save()
                return result

            total_bytes = (
                sum(info["size"] for _, info in to_upload)
                + sum(info.get("size", 0) for _, info in to_download)
            )

            with Progress() as progress:
                task = progress.add_task("[cyan]Syncing...", total=total_bytes)

                # Upload files
                for rel_path, info in to_upload:
                    local_file = local_path / rel_path
                    remote_file = f"{remote_path}/{rel_path}"

                    try:
                        remote_dir = posixpath.dirname(remote_file)
                        self._mkdir_p(remote_dir)
                        self.sftp.put(str(local_file), remote_file)
                        progress.update(task, advance=info["size"])

                        file_stat = local_file.stat()
                        self._remote_manifest.set(rel_path, info["hash"], file_stat.st_mtime, info["size"])

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
                        self.sftp.get(remote_file, str(local_file))
                        progress.update(task, advance=info.get("size", 0))

                        # Update cache and manifest
                        new_hash = self._hash_cache.get_hash(rel_path, local_file)
                        file_stat = local_file.stat()
                        self._remote_manifest.set(rel_path, new_hash, file_stat.st_mtime, file_stat.st_size)

                        result.files_downloaded += 1
                        result.bytes_transferred += info.get("size", 0)
                    except Exception as e:
                        result.errors.append(f"Failed to download {rel_path}: {e}")

            # Handle deletions
            if options.direction == SyncDirection.PUSH and options.delete_remote:
                for rel_path in remote_files:
                    if rel_path not in local_files:
                        remote_file = f"{remote_path}/{rel_path}"
                        try:
                            self.sftp.remove(remote_file)
                            self._remote_manifest.remove(rel_path)
                            result.files_deleted += 1
                        except Exception:
                            pass
                self._cleanup_empty_dirs(remote_path)

            elif options.direction == SyncDirection.PULL and options.delete_local:
                for rel_path in local_files:
                    if rel_path not in remote_files:
                        local_file = local_path / rel_path
                        try:
                            local_file.unlink()
                            self._hash_cache.invalidate(rel_path)
                            result.files_deleted += 1
                        except Exception:
                            pass
                self._cleanup_empty_local_dirs(local_path)

            self._hash_cache.save_cache()
            self._remote_manifest.save()

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
        """
        Get all local files with their hashes using the cache.

        Args:
            root: Root directory to scan
            exclude_patterns: Patterns to exclude

        Returns:
            Dict mapping relative paths to file info (path, hash, size, mtime)
        """
        files = {}

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            rel_path = path.relative_to(root)
            rel_path_posix = rel_path.as_posix()

            if self._should_exclude(rel_path_posix, exclude_patterns):
                continue

            # Use cached hash if available
            file_hash = self._hash_cache.get_hash(rel_path_posix, path)
            file_stat = path.stat()

            files[rel_path_posix] = {
                "path": path,
                "hash": file_hash,
                "size": file_stat.st_size,
                "mtime": file_stat.st_mtime,
            }

        return files

    def _get_remote_files(self, remote_path: str, exclude_patterns: list[str] = None) -> dict[str, dict]:
        """
        Get all remote files with their metadata.

        Args:
            remote_path: Remote directory to scan
            exclude_patterns: Patterns to exclude (optional)

        Returns:
            Dict mapping relative paths to file info (size, mtime)
        """
        if exclude_patterns is None:
            exclude_patterns = []

        files = {}

        try:
            self._walk_remote(remote_path, "", files, exclude_patterns)
        except IOError:
            pass

        return files

    def _walk_remote(
        self, base_path: str, rel_path: str, files: dict[str, dict], exclude_patterns: list[str]
    ) -> None:
        """
        Recursively walk remote directory.

        Args:
            base_path: Base remote path
            rel_path: Current relative path
            files: Dict to populate with file info
            exclude_patterns: Patterns to exclude
        """
        current_path = f"{base_path}/{rel_path}" if rel_path else base_path

        try:
            for entry in self.sftp.listdir_attr(current_path):
                entry_rel = f"{rel_path}/{entry.filename}" if rel_path else entry.filename

                # Check exclusions
                if self._should_exclude(entry_rel, exclude_patterns):
                    continue

                if stat.S_ISDIR(entry.st_mode):
                    self._walk_remote(base_path, entry_rel, files, exclude_patterns)
                else:
                    files[entry_rel] = {
                        "size": entry.st_size,
                        "mtime": entry.st_mtime,
                    }
        except IOError:
            pass

    def _should_exclude(self, path: str, patterns: list[str]) -> bool:
        """
        Check if path matches any exclusion pattern.

        Args:
            path: POSIX-style path (forward slashes) to check
            patterns: List of glob patterns to match against

        Returns:
            True if path should be excluded
        """
        import fnmatch

        parts = path.split("/")
        for pattern in patterns:
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def _hash_file(self, path: Path) -> str:
        """
        Calculate SHA256 hash of a file.

        Args:
            path: Path to the file

        Returns:
            SHA256 hash as hex string
        """
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _mkdir_p(self, remote_path: str) -> None:
        """
        Create remote directory and parents if needed.

        Args:
            remote_path: Remote directory path to create
        """
        if not remote_path or remote_path == "/":
            return

        try:
            self.sftp.stat(remote_path)
        except IOError:
            parent = posixpath.dirname(remote_path)
            if parent:
                self._mkdir_p(parent)
            try:
                self.sftp.mkdir(remote_path)
            except IOError:
                pass

    def _cleanup_empty_dirs(self, remote_path: str) -> None:
        """
        Remove empty directories on remote.

        Args:
            remote_path: Remote directory to clean up
        """
        try:
            for entry in self.sftp.listdir_attr(remote_path):
                if stat.S_ISDIR(entry.st_mode):
                    subdir = f"{remote_path}/{entry.filename}"
                    self._cleanup_empty_dirs(subdir)
                    try:
                        self.sftp.rmdir(subdir)
                    except IOError:
                        pass
        except IOError:
            pass

    def _cleanup_empty_local_dirs(self, local_path: Path) -> None:
        """
        Remove empty directories locally.

        Args:
            local_path: Local directory to clean up
        """
        for dirpath in sorted(local_path.rglob("*"), reverse=True):
            if dirpath.is_dir():
                try:
                    dirpath.rmdir()  # Only succeeds if empty
                except OSError:
                    pass
