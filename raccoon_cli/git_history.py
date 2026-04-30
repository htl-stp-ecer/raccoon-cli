"""Local git history helpers for project scaffolding and sync snapshots."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

AUTO_GIT_USER_NAME = "Raccoon Auto History"
AUTO_GIT_USER_EMAIL = "raccoon@local"


@dataclass
class GitInitResult:
    """Result of project git initialization."""

    initialized: bool
    commit_created: bool
    commit_sha: str | None = None
    reason: str = ""
    error: str = ""


@dataclass
class GitSnapshotResult:
    """Result of an automatic pre-sync snapshot commit."""

    created: bool
    commit_sha: str | None = None
    summary: str = ""
    reason: str = ""
    error: str = ""

    @property
    def short_sha(self) -> str | None:
        return self.commit_sha[:7] if self.commit_sha else None


def _run_git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
    )


def is_git_available() -> bool:
    """Return True if git is available in PATH."""
    return shutil.which("git") is not None


def is_git_repo(project_root: Path) -> bool:
    """Return True if project_root is inside a git repository."""
    if not is_git_available():
        return False

    proc = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _has_staged_changes(project_root: Path) -> tuple[bool, str]:
    """Return whether the index has staged changes."""
    proc = _run_git(project_root, ["diff", "--cached", "--quiet"])
    if proc.returncode == 1:
        return True, ""
    if proc.returncode == 0:
        return False, ""
    return False, proc.stderr.strip() or proc.stdout.strip()


def _collect_staged_changes(project_root: Path) -> tuple[list[tuple[str, str]], str]:
    """
    Return staged changes as (status, path) tuples.

    Status uses the git short name-status code (A, M, D, ...).
    """
    proc = _run_git(project_root, ["diff", "--cached", "--name-status", "--no-renames"])
    if proc.returncode != 0:
        return [], proc.stderr.strip() or proc.stdout.strip()

    entries: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][:1]
        path = parts[-1]
        entries.append((status, path))
    return entries, ""


def _build_change_summary(changes: list[tuple[str, str]]) -> str:
    added = sum(1 for status, _ in changes if status == "A")
    modified = sum(1 for status, _ in changes if status == "M")
    deleted = sum(1 for status, _ in changes if status == "D")
    other = len(changes) - added - modified - deleted

    summary = f"{len(changes)} files (+{added} ~{modified} -{deleted}"
    if other:
        summary += f" ?{other}"
    return summary + ")"


def _commit(
    project_root: Path,
    subject: str,
    body: str | None = None,
) -> tuple[str | None, str]:
    args = [
        "-c",
        f"user.name={AUTO_GIT_USER_NAME}",
        "-c",
        f"user.email={AUTO_GIT_USER_EMAIL}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-m",
        subject,
    ]
    if body:
        args.extend(["-m", body])

    commit_proc = _run_git(project_root, args)
    if commit_proc.returncode != 0:
        return None, commit_proc.stderr.strip() or commit_proc.stdout.strip()

    sha_proc = _run_git(project_root, ["rev-parse", "--short", "HEAD"])
    if sha_proc.returncode != 0:
        return None, sha_proc.stderr.strip() or sha_proc.stdout.strip()

    return sha_proc.stdout.strip(), ""


def initialize_project_history(project_root: Path, project_name: str) -> GitInitResult:
    """Initialize a local git repository and create an initial scaffold commit."""
    if not is_git_available():
        return GitInitResult(initialized=False, commit_created=False, reason="git_unavailable")

    if is_git_repo(project_root):
        return GitInitResult(initialized=False, commit_created=False, reason="already_git_repo")

    init_proc = _run_git(project_root, ["init", "--initial-branch", "main"])
    if init_proc.returncode != 0:
        init_proc = _run_git(project_root, ["init"])
        if init_proc.returncode != 0:
            return GitInitResult(
                initialized=False,
                commit_created=False,
                reason="init_failed",
                error=init_proc.stderr.strip() or init_proc.stdout.strip(),
            )

    add_proc = _run_git(project_root, ["add", "-A"])
    if add_proc.returncode != 0:
        return GitInitResult(
            initialized=True,
            commit_created=False,
            reason="add_failed",
            error=add_proc.stderr.strip() or add_proc.stdout.strip(),
        )

    has_changes, check_error = _has_staged_changes(project_root)
    if check_error:
        return GitInitResult(
            initialized=True,
            commit_created=False,
            reason="status_failed",
            error=check_error,
        )

    if not has_changes:
        return GitInitResult(initialized=True, commit_created=False, reason="no_changes")

    subject = f"chore(project): scaffold {project_name}"
    body = "Initial project scaffold generated by `raccoon create project`."
    commit_sha, commit_error = _commit(project_root, subject, body=body)
    if commit_error:
        return GitInitResult(
            initialized=True,
            commit_created=False,
            reason="commit_failed",
            error=commit_error,
        )

    return GitInitResult(
        initialized=True,
        commit_created=True,
        commit_sha=commit_sha,
    )


def create_pre_sync_snapshot(project_root: Path, direction: str, target: str) -> GitSnapshotResult:
    """Create an automatic snapshot commit before sync if local changes exist."""
    if not is_git_available():
        return GitSnapshotResult(created=False, reason="git_unavailable")

    if not is_git_repo(project_root):
        return GitSnapshotResult(created=False, reason="not_git_repo")

    add_proc = _run_git(project_root, ["add", "-A"])
    if add_proc.returncode != 0:
        return GitSnapshotResult(
            created=False,
            reason="add_failed",
            error=add_proc.stderr.strip() or add_proc.stdout.strip(),
        )

    has_changes, check_error = _has_staged_changes(project_root)
    if check_error:
        return GitSnapshotResult(
            created=False,
            reason="status_failed",
            error=check_error,
        )
    if not has_changes:
        return GitSnapshotResult(created=False, reason="no_changes")

    changes, changes_error = _collect_staged_changes(project_root)
    if changes_error:
        return GitSnapshotResult(
            created=False,
            reason="changes_failed",
            error=changes_error,
        )

    summary = _build_change_summary(changes)
    subject = f"chore(sync): pre-{direction} snapshot"
    body_lines = [
        f"Direction: {direction}",
        f"Target: {target}",
        f"Summary: {summary}",
        "",
        "Changed files:",
    ]

    max_paths = 12
    for status, path in changes[:max_paths]:
        body_lines.append(f"- {status} {path}")
    remaining = len(changes) - max_paths
    if remaining > 0:
        body_lines.append(f"- ... (+{remaining} more)")

    commit_sha, commit_error = _commit(project_root, subject, body="\n".join(body_lines))
    if commit_error:
        return GitSnapshotResult(
            created=False,
            reason="commit_failed",
            error=commit_error,
        )

    return GitSnapshotResult(
        created=True,
        commit_sha=commit_sha,
        summary=summary,
    )
