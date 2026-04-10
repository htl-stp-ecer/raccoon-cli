"""Deterministic content fingerprints for project trees.

Used to verify that a local project and its remote counterpart on the Pi are
byte-identical after a sync. The same walk + hash logic runs on both sides;
if the resulting root hashes match, every synced file has the same content.

Hashing rules:
- Walk the tree rooted at ``root`` with the same exclude patterns the sync uses.
- Hash every file that survives the exclude filter — nothing else is carved out.
  Generated files (codegen outputs, etc.) are included on purpose, so a fingerprint
  mismatch after codegen runs on the Pi is a real mismatch, not a spurious one.
- Symlinks are skipped: they are platform-dependent and not something we sync.
- Directories contribute nothing (empty dirs are invisible to the fingerprint).
- Per-file contribution is ``sha256(file_bytes)``; paths use POSIX separators.
- The root hash is ``sha256`` over ``"{relpath}\\0{filehash}\\n"`` lines, in
  sorted order of ``relpath``. Sorted input makes the result order-independent.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path


CHUNK_SIZE = 1024 * 1024  # 1 MiB


@dataclass
class FingerprintResult:
    """Result of fingerprinting a project tree."""

    root_hash: str  # sha256 hex of the whole tree
    files: dict[str, str] = field(default_factory=dict)  # relpath (posix) -> sha256 hex
    total_bytes: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)

    def diff(self, other: "FingerprintResult") -> dict[str, list[str]]:
        """Return per-bucket differences vs. ``other``.

        Buckets:
            - ``only_in_self``: present here, missing in ``other``
            - ``only_in_other``: present in ``other``, missing here
            - ``changed``: present on both sides but file hash differs
        """
        self_keys = set(self.files)
        other_keys = set(other.files)
        return {
            "only_in_self": sorted(self_keys - other_keys),
            "only_in_other": sorted(other_keys - self_keys),
            "changed": sorted(
                k for k in self_keys & other_keys if self.files[k] != other.files[k]
            ),
        }


def _should_exclude(rel_path: str, patterns: list[str]) -> bool:
    """Component-wise fnmatch, matching :func:`raccoon.client.sftp_sync._should_exclude`."""
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts:
        for pat in patterns:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _hash_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for a regular file."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def compute_fingerprint(
    root: Path,
    exclude_patterns: list[str] | None = None,
) -> FingerprintResult:
    """Compute a deterministic fingerprint for the tree under ``root``.

    Args:
        root: Directory to fingerprint.
        exclude_patterns: fnmatch patterns applied component-wise to each path,
            same semantics as the sync backends. ``None`` means no excludes.

    Returns:
        A :class:`FingerprintResult` with the root hash, per-file hashes, and
        total bytes hashed.
    """
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Fingerprint root does not exist: {root}")

    patterns = list(exclude_patterns or [])
    files: dict[str, str] = {}
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        # Prune excluded directories in-place so os.walk skips them.
        dirnames[:] = sorted(
            d for d in dirnames
            if not _should_exclude(f"{rel_dir}/{d}" if rel_dir else d, patterns)
        )

        for fname in sorted(filenames):
            rel_posix = f"{rel_dir}/{fname}" if rel_dir else fname
            if _should_exclude(rel_posix, patterns):
                continue

            abs_path = Path(dirpath) / fname
            # Skip symlinks — platform-dependent, not part of the fingerprint.
            try:
                if abs_path.is_symlink():
                    continue
                if not abs_path.is_file():
                    continue
            except OSError:
                continue

            file_hash, size = _hash_file(abs_path)
            files[rel_posix] = file_hash
            total_bytes += size

    # Combine per-file hashes into a single root hash, deterministically.
    h = hashlib.sha256()
    for rel_posix in sorted(files):
        h.update(f"{rel_posix}\0{files[rel_posix]}\n".encode("utf-8"))
    root_hash = h.hexdigest()

    return FingerprintResult(root_hash=root_hash, files=files, total_bytes=total_bytes)


def default_exclude_patterns() -> list[str]:
    """Return the default exclude list used by sync, for callers that want parity.

    This mirrors :class:`raccoon.client.sftp_sync.SyncOptions` defaults.
    Callers should also union in patterns from ``.raccoonignore``.
    """
    return [
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
