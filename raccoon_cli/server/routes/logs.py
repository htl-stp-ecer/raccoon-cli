"""Log browsing API routes — serve parsed log runs from the Pi."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from raccoon_cli.logs import (
    DEFAULT_LIST_LIMIT,
    current_log_file,
    discover_log_files,
    load_run_by_index,
    load_runs,
)
from raccoon_cli.logs.cmd_trace import (
    load_cmd_trace,
    resolve_cmd_trace_path,
    run_window_us,
    slice_cmd_trace,
)
from raccoon_cli.logs.journal import (
    bundle_journal_units,
    collect_journals,
    journal_file_body,
    journal_manifest_section,
    journalctl_lines,
)
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
    # Every run lives in .raccoon/runs/<run_id>/ (unified per-run dirs), each with
    # its own libstp.jsonl. 404 only when there are no runs at all.
    log_dir = project_path / ".raccoon" / "runs"
    if current_log_file(log_dir) is None:
        raise HTTPException(status_code=404, detail="No logs found for this project")
    return log_dir


def _load_runs(log_dir: Path, limit: Optional[int] = None):
    # Every run is its own JSONL file now, so this returns all recent runs.
    # ``limit`` caps how many of the newest files are parsed so listing stays
    # fast on projects with many runs.
    return load_runs(discover_log_files(log_dir), limit=limit)


@router.get("/{project_id}/runs")
async def list_runs(
    project_id: str,
    include_rotated: bool = Query(False, alias="all"),  # retained for the ?all= alias
    count: Optional[int] = Query(None, alias="n"),
):
    """List detected log runs for a project."""
    log_dir = _get_log_dir_or_404(project_id)

    # Parse only the newest files: an explicit ``n`` (what the caller will show)
    # or the default cap. Older runs are still reachable by explicit index.
    total_files = len(discover_log_files(log_dir))
    parse_limit = count if count else DEFAULT_LIST_LIMIT
    runs = await asyncio.to_thread(_load_runs, log_dir, parse_limit)

    if count:
        runs = sorted(runs, key=lambda r: r.index)[:count]

    return {
        "project_id": project_id,
        "log_dir": str(log_dir),
        "total_runs": total_files,
        "loaded_runs": len(runs),
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
    files = discover_log_files(log_dir)
    run = await asyncio.to_thread(load_run_by_index, files, run_index)
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
                "line": e.line_number,
                "func": e.func,
                "message": e.message,
            }
            for e in entries
        ],
    }


_CANONICAL_ARTIFACTS = (
    "libstp.jsonl", "localization.jsonl", "profile.json", "run.json", "sensors.mcap",
)


def _artifact_entries(sizes: dict[str, int]) -> list[dict]:
    """Bundle-manifest artifact list from a name→size map (canonical + extras)."""
    entries: list[dict] = []
    listed: set[str] = set()
    for name in _CANONICAL_ARTIFACTS:
        entries.append(
            {"name": name, "size": sizes.get(name, 0), "present": name in sizes}
        )
        listed.add(name)
    for name in sorted(sizes):
        if name not in listed:
            entries.append({"name": name, "size": sizes[name], "present": True})
    return entries


def _build_run_bundle_zip(
    run, run_dir: Path, cmd_trace: dict, journals: list[dict]
) -> bytes:
    """Zip the run dir + cmd_trace slice + service journals + manifest; bytes."""
    trace_body = "".join(
        json.dumps(entry) + "\n" for entry in cmd_trace.get("entries", [])
    )
    run_meta = {
        "index": run.index,
        "run_id": run.run_id,
        "start_time": run.start_time.isoformat(),
        "end_time": run.end_time.isoformat(),
        "duration_secs": run.duration_secs,
        "line_count": run.line_count,
    }

    buf = io.BytesIO()
    sizes: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(run_dir.iterdir()):
            # Skip hidden sidecars (e.g. the .libstp.jsonl.meta.json cache).
            if f.is_file() and not f.name.startswith("."):
                data = f.read_bytes()
                zf.writestr(f.name, data)
                sizes[f.name] = len(data)
        zf.writestr("cmd_trace.jsonl", trace_body)
        sizes["cmd_trace.jsonl"] = len(trace_body.encode("utf-8"))

        for section in journals:
            body = journal_file_body(section)
            zf.writestr(section["file"], body)
            sizes[section["file"]] = len(body.encode("utf-8"))

        manifest = {
            "run": run_meta,
            "artifacts": _artifact_entries(sizes),
            "cmd_trace": {
                "file": "cmd_trace.jsonl",
                "source_path": cmd_trace.get("path"),
                "available": cmd_trace.get("available", False),
                "total_lines": cmd_trace.get("total_lines", 0),
                "matched_lines": cmd_trace.get("matched_lines", 0),
                "window_start_us": cmd_trace.get("window_start_us"),
                "window_end_us": cmd_trace.get("window_end_us"),
                "pad_secs": cmd_trace.get("pad_secs"),
            },
            "journals": [journal_manifest_section(s) for s in journals],
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()


@router.get("/{project_id}/runs/{run_index}/bundle")
async def get_run_bundle(
    project_id: str,
    run_index: int,
    pad_secs: float = Query(2.0, alias="pad", ge=0, le=60),
):
    """Zip a run's whole artifact directory + the STM32 cmd_trace slice.

    Returns ``application/zip`` containing every file in the run's
    ``.raccoon/runs/<run_id>/`` directory (log, localization, profile, manifest)
    plus the stm32-data-reader command trace (``cmd_trace.jsonl``) filtered to
    the run's wall-clock window and a ``manifest.json`` describing the bundle.
    The trace is truncated on each reader restart, so it only overlaps the run
    when the reader wasn't restarted afterwards; ``matched_lines`` reports how
    much actually fell inside the window.

    The bundle also carries the journald output — sliced to the same window — of
    every service raccoon manages: the raccoon-server, the stm32-data-reader, and
    each service declared in the project's ``raccoon.project.yml``. Each lands as
    ``journal.<service>.jsonl`` with a ``journals`` manifest section.
    """
    project_path = _get_project_path_or_404(project_id)
    log_dir = _get_log_dir_or_404(project_id)
    files = discover_log_files(log_dir)
    run = await asyncio.to_thread(load_run_by_index, files, run_index)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run #{run_index} not found")
    if not run.run_dir:
        raise HTTPException(
            status_code=404,
            detail=f"Run #{run_index} has no unified run directory to bundle",
        )
    run_dir = Path(run.run_dir)

    start_us, end_us = run_window_us(run.start_time, run.end_time, pad_secs)
    trace_path = resolve_cmd_trace_path()
    cmd_trace: dict = {
        "path": str(trace_path),
        "available": False,
        "total_lines": 0,
        "matched_lines": 0,
        "window_start_us": start_us,
        "window_end_us": end_us,
        "pad_secs": pad_secs,
        "entries": [],
    }
    if trace_path.is_file():
        records = await asyncio.to_thread(load_cmd_trace, trace_path)
        matched = slice_cmd_trace(records, start_us, end_us)
        cmd_trace.update(
            available=True,
            total_lines=len(records),
            matched_lines=len(matched),
            entries=matched,
        )

    units = bundle_journal_units(project_path)
    journals = await asyncio.to_thread(collect_journals, units, start_us, end_us)

    zip_bytes = await asyncio.to_thread(
        _build_run_bundle_zip, run, run_dir, cmd_trace, journals
    )
    filename = f"run-{run.run_id or run_index}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        entries = await asyncio.to_thread(journalctl_lines, svc.systemd_name, lines)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "project_id": project_id,
        "service": {"name": svc.name, "systemd_name": svc.systemd_name},
        "lines": lines,
        "entries": entries,
    }
