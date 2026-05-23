"""Filesystem-backed repository for post-run localization recordings.

Each project has a ``.raccoon/runs/`` directory at its project root. Inside, every
run gets its own subdirectory named ``<UTC-ISO-timestamp>`` (compact form
``YYYYMMDDThhmmssZ``). The recorder writes ``localization.jsonl`` into that
directory while a run is in progress.

This repository exposes a thin service layer for the IDE backend (and any
future CLI consumer) to list, inspect, stream, and delete those recordings.
All logic that interprets the ``.raccoon/runs/`` directory layout lives here -- the
FastAPI routes are thin wrappers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Compact UTC-ISO timestamp form used as the run directory name.
# Example: 20260523T143012Z
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")

_RUNS_DIRNAME = ".raccoon/runs"
_LOCALIZATION_FILENAME = "localization.jsonl"


def _parse_run_id(run_id: str) -> datetime:
    """Parse a run id of the form ``YYYYMMDDThhmmssZ`` to a UTC datetime.

    Raises ``ValueError`` if the id does not match the expected format.
    """
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"Invalid run id: {run_id!r}")
    # %Y%m%dT%H%M%SZ
    dt = datetime.strptime(run_id, "%Y%m%dT%H%M%SZ")
    return dt.replace(tzinfo=timezone.utc)


def _validate_run_id(run_id: str) -> None:
    """Strict validation -- call this before constructing any path.

    The run id is treated as untrusted input. We refuse anything that does
    not match the exact compact UTC timestamp form, so path-traversal
    attempts like ``..`` or ``foo/bar`` are rejected before we touch the
    filesystem.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"Invalid run id: {run_id!r}")


@dataclass
class RunSummary:
    """Lightweight metadata for a single recorded run."""

    run_id: str
    started_at: datetime
    has_localization: bool
    file_size_bytes: int
    frame_count: Optional[int] = None
    duration_ms: Optional[int] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        # JSON-friendly ISO 8601 with Z suffix
        data["started_at"] = self.started_at.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return data


class RunRepository:
    """Manage the ``.raccoon/runs/`` hierarchy for projects."""

    def __init__(self, projects_root: Path | str):
        self.projects_root = Path(projects_root)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _project_path(self, project_uuid: str) -> Path:
        """Locate the project directory by uuid.

        Project lookup is the responsibility of ``ProjectRepository``; in
        this lightweight repository we accept the uuid as a string and
        find the matching project directory by reading the
        ``raccoon.project.yml`` files under ``projects_root``.

        Returns the directory or raises ``FileNotFoundError`` if it does
        not exist.
        """
        try:
            parsed = uuid.UUID(str(project_uuid))
        except (ValueError, TypeError) as exc:
            raise FileNotFoundError(f"Project not found: {project_uuid}") from exc

        # Fast path: the convention used by ``create_project`` is to name
        # the directory after the project's display name, so we resolve by
        # scanning project configs. Keep this dependency-free to avoid
        # circular imports with ProjectRepository.
        from raccoon_cli.yaml_utils import load_yaml

        if not self.projects_root.exists():
            raise FileNotFoundError(f"Project not found: {project_uuid}")

        for child in self.projects_root.iterdir():
            if not child.is_dir():
                continue
            config = child / "raccoon.project.yml"
            if not config.exists():
                continue
            try:
                data = load_yaml(config)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            cfg_uuid = data.get("uuid")
            try:
                cfg_parsed = (
                    cfg_uuid
                    if isinstance(cfg_uuid, uuid.UUID)
                    else uuid.UUID(str(cfg_uuid))
                )
            except (ValueError, TypeError):
                continue
            if cfg_parsed == parsed:
                return child

        raise FileNotFoundError(f"Project not found: {project_uuid}")

    def _runs_root(self, project_uuid: str) -> Path:
        return self._project_path(project_uuid) / _RUNS_DIRNAME

    def get_localization_path(self, project_uuid: str, run_id: str) -> Path:
        """Return the path to the localization.jsonl for the given run.

        Validates ``run_id`` strictly to prevent path traversal. Raises
        ``FileNotFoundError`` if the recording file is missing.
        """
        _validate_run_id(run_id)
        runs_root = self._runs_root(project_uuid)
        run_dir = runs_root / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run not found: {run_id}")
        file_path = run_dir / _LOCALIZATION_FILENAME
        if not file_path.is_file():
            raise FileNotFoundError(
                f"recording_missing: {run_id}/{_LOCALIZATION_FILENAME}"
            )
        return file_path

    # ------------------------------------------------------------------
    # Listing / inspection
    # ------------------------------------------------------------------

    def list_runs(self, project_uuid: str) -> List[RunSummary]:
        """List runs that have a localization recording.

        Directories under ``.raccoon/runs/`` with an invalid name are skipped
        silently -- they may be in-progress writes, manually-created junk,
        or recordings from a future format. Sorted newest-first.
        """
        runs_root = self._runs_root(project_uuid)
        if not runs_root.is_dir():
            return []

        summaries: List[RunSummary] = []
        for entry in runs_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                started_at = _parse_run_id(entry.name)
            except ValueError:
                continue
            file_path = entry / _LOCALIZATION_FILENAME
            has_localization = file_path.is_file()
            if not has_localization:
                # Brief says: only return runs with has_localization=True.
                continue
            size = file_path.stat().st_size
            summaries.append(
                RunSummary(
                    run_id=entry.name,
                    started_at=started_at,
                    has_localization=True,
                    file_size_bytes=size,
                )
            )

        summaries.sort(key=lambda s: s.run_id, reverse=True)
        return summaries

    def get_run_metadata(self, project_uuid: str, run_id: str) -> RunSummary:
        """Return a summary with lazy fields (frame_count, duration_ms) filled in."""
        _validate_run_id(run_id)
        runs_root = self._runs_root(project_uuid)
        run_dir = runs_root / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run not found: {run_id}")

        started_at = _parse_run_id(run_id)
        file_path = run_dir / _LOCALIZATION_FILENAME
        if not file_path.is_file():
            return RunSummary(
                run_id=run_id,
                started_at=started_at,
                has_localization=False,
                file_size_bytes=0,
            )

        stat = file_path.stat()
        frame_count = _count_lines(file_path) - 1  # subtract header line
        if frame_count < 0:
            frame_count = 0
        duration_ms = _read_duration_ms(file_path)

        return RunSummary(
            run_id=run_id,
            started_at=started_at,
            has_localization=True,
            file_size_bytes=stat.st_size,
            frame_count=frame_count,
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def delete_run(self, project_uuid: str, run_id: str) -> None:
        """Delete a single ``.raccoon/runs/<run_id>/`` directory.

        Strict validation on ``run_id`` happens before any path is
        constructed, so traversal attempts cannot escape ``.raccoon/runs/``.
        """
        _validate_run_id(run_id)
        runs_root = self._runs_root(project_uuid)
        run_dir = runs_root / run_id
        # Defensive: refuse to delete anything that escaped the runs root.
        try:
            resolved = run_dir.resolve()
            runs_resolved = runs_root.resolve()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Run not found: {run_id}") from exc
        if runs_resolved not in resolved.parents:
            raise ValueError(f"Refusing to delete outside .raccoon/runs: {run_id}")
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run not found: {run_id}")
        shutil.rmtree(run_dir)


# ----------------------------------------------------------------------
# Internal helpers -- frame-counting and last-line parsing.
# ----------------------------------------------------------------------


def _count_lines(path: Path) -> int:
    """Count newline-terminated lines in *path*.

    We don't load the whole file -- iterating in a binary mode buffer is
    O(file size) but allocates only a fixed buffer at a time.
    """
    count = 0
    with path.open("rb") as fh:
        buf_size = 1 << 16
        while True:
            chunk = fh.read(buf_size)
            if not chunk:
                break
            count += chunk.count(b"\n")
    # If the last line lacks a trailing newline, count it too.
    with path.open("rb") as fh:
        try:
            fh.seek(-1, os.SEEK_END)
            last = fh.read(1)
            if last and last != b"\n":
                count += 1
        except OSError:
            # Empty file
            pass
    return count


def _read_last_line(path: Path) -> Optional[bytes]:
    """Return the last non-empty line of a file, reading backwards.

    Returns ``None`` for empty files. Reads in 4 KB blocks from the tail,
    stopping as soon as a newline boundary is found -- so memory usage is
    independent of file size.
    """
    block_size = 4096
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        if end == 0:
            return None

        # Strip trailing newlines so an empty terminal line isn't returned.
        pos = end
        tail_buf = b""
        while pos > 0:
            read_size = min(block_size, pos)
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            tail_buf = chunk + tail_buf
            # Strip any trailing newlines, then find the previous \n.
            stripped = tail_buf.rstrip(b"\n")
            if not stripped:
                # Whole buffer so far is newlines; need to read further back.
                if pos == 0:
                    return None
                continue
            nl_idx = stripped.rfind(b"\n")
            if nl_idx != -1:
                return stripped[nl_idx + 1 :]
            if pos == 0:
                return stripped
        return None


def _read_duration_ms(path: Path) -> Optional[int]:
    """Read ``t_ns`` from the last frame in the file and convert to ms.

    Returns ``None`` if the last line cannot be parsed or has no ``t_ns``.
    """
    last = _read_last_line(path)
    if last is None:
        return None
    try:
        obj = json.loads(last.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    t_ns = obj.get("t_ns")
    if not isinstance(t_ns, (int, float)):
        return None
    return int(t_ns // 1_000_000)
