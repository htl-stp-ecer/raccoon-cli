"""Log browsing API routes — serve parsed log runs from the Pi."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from raccoon_cli.logs import detect_runs, discover_log_files, parse_log_file
from raccoon_cli.server.auth import require_auth

router = APIRouter(prefix="/api/v1/logs", tags=["logs"], dependencies=[Depends(require_auth)])


def _get_project_path_or_404(project_id: str) -> Path:
    from raccoon_cli.server.app import get_config
    from raccoon_cli.server.services.project_manager import ProjectManager

    manager = ProjectManager(get_config().projects_dir)
    project_path = manager.get_project_path(project_id)
    if project_path is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project_path


def _get_log_dir_or_404(project_id: str) -> Path:
    project_path = _get_project_path_or_404(project_id)
    log_dir = project_path / "logs"
    if not log_dir.is_dir() or not (log_dir / "libstp.log").exists():
        raise HTTPException(status_code=404, detail="No logs directory found for this project")
    return log_dir


def _load_runs(log_dir: Path, include_rotated: bool = False):
    files = discover_log_files(log_dir)
    if not include_rotated:
        current = log_dir / "libstp.log"
        files = [f for f in files if f == current]
    all_entries = []
    for f in files:
        all_entries.extend(parse_log_file(f))
    return detect_runs(all_entries)


@router.get("/{project_id}/runs")
async def list_runs(
    project_id: str,
    include_rotated: bool = Query(False, alias="all"),
    count: Optional[int] = Query(None, alias="n"),
):
    """List detected log runs for a project."""
    log_dir = _get_log_dir_or_404(project_id)
    runs = _load_runs(log_dir, include_rotated=include_rotated)

    if count:
        runs = sorted(runs, key=lambda r: r.index)[:count]

    return {
        "project_id": project_id,
        "log_dir": str(log_dir),
        "runs": [
            {
                "index": r.index,
                "start_time": r.start_time.isoformat(),
                "end_time": r.end_time.isoformat(),
                "duration_secs": r.duration_secs,
                "line_count": r.line_count,
                "level_counts": r.level_counts,
                "sources": sorted(r.sources),
            }
            for r in sorted(runs, key=lambda r: r.index)
        ],
    }


@router.get("/{project_id}/runs/{run_index}")
async def get_run(
    project_id: str,
    run_index: int,
    level: Optional[str] = None,
    source: Optional[str] = None,
    grep: Optional[str] = None,
    include_rotated: bool = Query(False, alias="all"),
):
    """Get log entries for a specific run, with optional filtering."""
    import re as re_mod

    log_dir = _get_log_dir_or_404(project_id)
    runs = _load_runs(log_dir, include_rotated=include_rotated)

    run = next((r for r in runs if r.index == run_index), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run #{run_index} not found")

    entries = run.entries

    if level:
        lvl = level.upper()
        if lvl == "WARNING":
            lvl = "WARN"
        entries = [e for e in entries if e.level_upper == lvl]

    if source:
        src_lower = source.lower()
        entries = [e for e in entries if src_lower in e.source.lower()]

    if grep:
        pattern = re_mod.compile(grep, re_mod.IGNORECASE)
        entries = [e for e in entries if pattern.search(e.message)]

    return {
        "project_id": project_id,
        "run": {
            "index": run.index,
            "start_time": run.start_time.isoformat(),
            "end_time": run.end_time.isoformat(),
            "duration_secs": run.duration_secs,
            "line_count": run.line_count,
        },
        "filtered_count": len(entries),
        "entries": [
            {
                "elapsed": e.elapsed,
                "level": e.level_upper,
                "source": e.source,
                "message": e.message,
            }
            for e in entries
        ],
    }


@router.delete("/{project_id}")
async def clear_logs(project_id: str):
    """Delete all log files for a project."""
    log_dir = _get_log_dir_or_404(project_id)
    files = discover_log_files(log_dir)

    deleted = 0
    total_bytes = 0
    for f in files:
        total_bytes += f.stat().st_size
        f.unlink()
        deleted += 1

    timing_db = log_dir / "step_timing.db"
    if timing_db.exists():
        total_bytes += timing_db.stat().st_size
        timing_db.unlink()

    return {
        "deleted_files": deleted,
        "total_bytes": total_bytes,
    }
