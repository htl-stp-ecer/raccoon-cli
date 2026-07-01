"""Log browsing API routes — serve parsed log runs from the Pi."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from raccoon_cli.logs import current_log_file, detect_runs, discover_log_files, parse_log_file
from raccoon_cli.project import load_project_config
from raccoon_cli.project_services import load_project_services
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
    log_dir = project_path / ".raccoon" / "logs"
    if not log_dir.is_dir() or current_log_file(log_dir) is None:
        raise HTTPException(status_code=404, detail="No logs directory found for this project")
    return log_dir


def _load_runs(log_dir: Path, include_rotated: bool = False):
    files = discover_log_files(log_dir)
    if not include_rotated:
        current = current_log_file(log_dir)
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


# ── Project services ────────────────────────────────────────────────


_SYSTEMCTL_PROPS = (
    "Id,ActiveState,SubState,LoadState,MainPID,NRestarts,"
    "ActiveEnterTimestamp,ActiveExitTimestamp"
)

# journald PRIORITY → log level name (RFC 5424)
_JOURNAL_PRIORITY = {
    "0": "EMERG",
    "1": "ALERT",
    "2": "CRITICAL",
    "3": "ERROR",
    "4": "WARN",
    "5": "NOTICE",
    "6": "INFO",
    "7": "DEBUG",
}


def _load_configured_services(project_path: Path):
    """Load services declared in raccoon.project.yml; empty list if none."""
    config = load_project_config(project_path)
    if not isinstance(config, dict):
        return []
    return load_project_services(config, project_path)


def _systemctl_show(unit: str) -> dict[str, str]:
    """Run ``systemctl show`` for a unit and return its key=value pairs."""
    proc = subprocess.run(
        ["systemctl", "show", unit, f"--property={_SYSTEMCTL_PROPS}"],
        capture_output=True,
        text=True,
    )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


@router.get("/{project_id}/services")
async def list_services(project_id: str):
    """List project-declared services with their current systemd status."""
    project_path = _get_project_path_or_404(project_id)
    try:
        services = await asyncio.to_thread(_load_configured_services, project_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read services: {exc}")

    result = []
    for svc in services:
        info = await asyncio.to_thread(_systemctl_show, svc.systemd_name)
        result.append(
            {
                "name": svc.name,
                "systemd_name": svc.systemd_name,
                "module": svc.module,
                "command": svc.command,
                "active_state": info.get("ActiveState", "unknown"),
                "sub_state": info.get("SubState", ""),
                "load_state": info.get("LoadState", ""),
                "main_pid": info.get("MainPID", "0"),
                "n_restarts": info.get("NRestarts", "0"),
                "active_enter_ts": info.get("ActiveEnterTimestamp", ""),
                "active_exit_ts": info.get("ActiveExitTimestamp", ""),
                "required_for_run": svc.required_for_run,
                "after_sync": svc.after_sync,
            }
        )
    return {"project_id": project_id, "services": result}


def _journalctl(unit: str, lines: int) -> list[dict]:
    """Fetch the last N journal entries for a unit as parsed JSON dicts."""
    proc = subprocess.run(
        [
            "journalctl",
            "-u",
            unit,
            "-n",
            str(lines),
            "--no-pager",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"journalctl exited {proc.returncode}")

    entries: list[dict] = []
    for raw in proc.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts_us = obj.get("__REALTIME_TIMESTAMP")
        try:
            ts_iso = ""
            if ts_us:
                from datetime import datetime, timezone

                ts_iso = datetime.fromtimestamp(
                    int(ts_us) / 1_000_000, tz=timezone.utc
                ).isoformat()
        except (ValueError, TypeError):
            ts_iso = ""
        entries.append(
            {
                "timestamp": ts_iso,
                "level": _JOURNAL_PRIORITY.get(str(obj.get("PRIORITY", "6")), "INFO"),
                "message": obj.get("MESSAGE", ""),
                "pid": obj.get("_PID", ""),
                "identifier": obj.get("SYSLOG_IDENTIFIER", ""),
            }
        )
    return entries


@router.get("/{project_id}/services/{service_name}/journal")
async def get_service_journal(
    project_id: str,
    service_name: str,
    lines: int = Query(200, ge=1, le=10000),
):
    """Fetch the last N journald entries for a project service."""
    project_path = _get_project_path_or_404(project_id)
    services = await asyncio.to_thread(_load_configured_services, project_path)
    svc = next((s for s in services if s.name == service_name), None)
    if svc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service_name}' not declared in raccoon.project.yml",
        )

    try:
        entries = await asyncio.to_thread(_journalctl, svc.systemd_name, lines)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "project_id": project_id,
        "service": {"name": svc.name, "systemd_name": svc.systemd_name},
        "lines": lines,
        "entries": entries,
    }
