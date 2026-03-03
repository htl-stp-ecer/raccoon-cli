"""Invisible git checkpoints using refs/raccoon/checkpoints/.

Creates checkpoint objects via ``git stash create`` (no side-effects on index,
working tree, or stash list) and stores them as lightweight refs that are
invisible to ``git log``, ``git branch``, and ``git stash list``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from raccoon.git_history import _run_git, is_git_available, is_git_repo

_REF_PREFIX = "refs/raccoon/checkpoints/"


@dataclass
class CheckpointResult:
    """Result of creating a checkpoint."""

    created: bool
    sha: str | None = None
    short_sha: str | None = None
    ref: str | None = None
    reason: str = ""
    error: str = ""


@dataclass
class Checkpoint:
    """A stored checkpoint entry."""

    ref: str = ""
    sha: str = ""
    short_sha: str = ""
    timestamp: int = 0
    label: str = ""


def _sanitize_label(label: str) -> str:
    """Sanitize a label to alphanumeric characters and hyphens."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", label).strip("-") or "checkpoint"


def create_checkpoint(project_root: Path, label: str = "checkpoint") -> CheckpointResult:
    """Create an invisible checkpoint of the current working tree state.

    Uses ``git stash create`` to build a commit object that captures both staged
    and unstaged changes, then stores a ref under ``refs/raccoon/checkpoints/``
    so it won't be garbage-collected.  The index, working tree, and stash list
    are left completely untouched.
    """
    if not is_git_available():
        return CheckpointResult(created=False, reason="git_unavailable")

    if not is_git_repo(project_root):
        return CheckpointResult(created=False, reason="not_git_repo")

    # Save the current index state so we can restore it after staging untracked files
    tree_proc = _run_git(project_root, ["write-tree"])
    if tree_proc.returncode != 0:
        return CheckpointResult(
            created=False, reason="write_tree_failed", error=tree_proc.stderr.strip()
        )
    original_tree = tree_proc.stdout.strip()

    # Stage everything (including untracked files) so stash create captures it all
    _run_git(project_root, ["add", "-A"])

    # git stash create returns empty output when there are no changes
    proc = _run_git(project_root, ["stash", "create"])
    stash_error = proc.stderr.strip() if proc.returncode != 0 else ""
    sha = proc.stdout.strip()

    # Restore the original index state regardless of stash outcome
    _run_git(project_root, ["read-tree", original_tree])

    if proc.returncode != 0:
        return CheckpointResult(
            created=False, reason="stash_create_failed", error=stash_error
        )

    if not sha:
        return CheckpointResult(created=False, reason="no_changes")

    # Build the ref name
    ts = int(time.time())
    safe_label = _sanitize_label(label)
    ref = f"{_REF_PREFIX}{ts}_{safe_label}"

    # Store the ref so the object isn't garbage-collected
    ref_proc = _run_git(project_root, ["update-ref", ref, sha])
    if ref_proc.returncode != 0:
        return CheckpointResult(
            created=False,
            reason="update_ref_failed",
            error=ref_proc.stderr.strip(),
        )

    short_proc = _run_git(project_root, ["rev-parse", "--short", sha])
    short_sha = short_proc.stdout.strip() if short_proc.returncode == 0 else sha[:7]

    return CheckpointResult(created=True, sha=sha, short_sha=short_sha, ref=ref)


def list_checkpoints(project_root: Path) -> list[Checkpoint]:
    """List all stored checkpoints, newest first."""
    if not is_git_available() or not is_git_repo(project_root):
        return []

    proc = _run_git(
        project_root,
        [
            "for-each-ref",
            "--sort=-refname",
            "--format=%(refname) %(objectname) %(objectname:short)",
            _REF_PREFIX,
        ],
    )
    if proc.returncode != 0:
        return []

    checkpoints: list[Checkpoint] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        ref, sha, short_sha = parts[0], parts[1], parts[2]

        # Parse timestamp and label from ref name
        suffix = ref[len(_REF_PREFIX) :]
        ts_str, _, label = suffix.partition("_")
        try:
            ts = int(ts_str)
        except ValueError:
            ts = 0

        checkpoints.append(
            Checkpoint(ref=ref, sha=sha, short_sha=short_sha, timestamp=ts, label=label)
        )

    return checkpoints


def _resolve_checkpoint(project_root: Path, identifier: str) -> Checkpoint | None:
    """Resolve a checkpoint by index (1-based) or short SHA."""
    checkpoints = list_checkpoints(project_root)
    if not checkpoints:
        return None

    # Try as 1-based index
    try:
        idx = int(identifier)
        if 1 <= idx <= len(checkpoints):
            return checkpoints[idx - 1]
    except ValueError:
        pass

    # Try as SHA prefix
    for cp in checkpoints:
        if cp.sha.startswith(identifier) or cp.short_sha.startswith(identifier):
            return cp

    return None


def show_checkpoint_diff(project_root: Path, identifier: str) -> tuple[str | None, str]:
    """Show the diff of a checkpoint.

    Returns (diff_text, error).
    """
    cp = _resolve_checkpoint(project_root, identifier)
    if cp is None:
        return None, f"Checkpoint '{identifier}' not found"

    proc = _run_git(project_root, ["stash", "show", "-p", cp.sha])
    if proc.returncode != 0:
        return None, proc.stderr.strip() or "Failed to show checkpoint diff"

    return proc.stdout, ""


def restore_checkpoint(project_root: Path, identifier: str) -> tuple[bool, str]:
    """Apply a checkpoint to the working tree.

    Returns (success, error).
    """
    cp = _resolve_checkpoint(project_root, identifier)
    if cp is None:
        return False, f"Checkpoint '{identifier}' not found"

    proc = _run_git(project_root, ["stash", "apply", cp.sha])
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "Failed to apply checkpoint"

    return True, ""


def delete_checkpoint(project_root: Path, identifier: str) -> tuple[bool, str]:
    """Delete a single checkpoint ref.

    Returns (success, error).
    """
    cp = _resolve_checkpoint(project_root, identifier)
    if cp is None:
        return False, f"Checkpoint '{identifier}' not found"

    proc = _run_git(project_root, ["update-ref", "-d", cp.ref])
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "Failed to delete checkpoint"

    return True, ""


def clean_checkpoints(
    project_root: Path,
    max_age_days: int | None = 7,
    delete_all: bool = False,
) -> tuple[int, str]:
    """Delete checkpoint refs older than *max_age_days* (or all if *delete_all*).

    Returns (count_deleted, error).
    """
    checkpoints = list_checkpoints(project_root)
    if not checkpoints:
        return 0, ""

    if delete_all:
        cutoff = float("inf")
    else:
        cutoff = time.time() - (max_age_days or 7) * 86400

    deleted = 0
    for cp in checkpoints:
        if delete_all or cp.timestamp < cutoff:
            proc = _run_git(project_root, ["update-ref", "-d", cp.ref])
            if proc.returncode != 0:
                return deleted, proc.stderr.strip() or f"Failed to delete {cp.ref}"
            deleted += 1

    return deleted, ""
