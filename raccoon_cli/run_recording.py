"""Per-run artifact directory: env injection + manifest.

Every ``raccoon run`` co-locates all of a run's artifacts under one directory
``<project_root>/.raccoon/runs/<run_id>/``:

- ``libstp.jsonl``      — the log (raccoon-lib's C++ logger writes it here when
                          ``LIBSTP_LOG_DIR`` names the run dir)
- ``localization.jsonl``— particle-filter recording (opt-out)
- ``profile.json``      — step profiler output (opt-out; may append
                          ``.<MissionName>`` for multi-mission runs)
- ``run.json``          — the manifest written here by ``raccoon run`` at start

``run_id`` is a compact UTC timestamp (``YYYYMMDDThhmmssZ``) allocated once per
run, matching ``ide.repositories.run_repository``'s directory convention.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
from pathlib import Path
from typing import Optional

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")

#: Basename of the run-scoped raw sensor recording (see logs/sensor_recorder.py).
SENSORS_FILENAME = "sensors.mcap"


def make_run_id() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_rel_dir(run_id: str) -> str:
    """Project-relative run directory, e.g. ``.raccoon/runs/<run_id>``."""
    return f".raccoon/runs/{run_id}"


def run_dir_path(project_path: Path, run_id: str) -> Path:
    """Absolute run directory under *project_path*."""
    return project_path / ".raccoon" / "runs" / run_id


def recording_rel_path(run_id: str) -> str:
    return f"{run_rel_dir(run_id)}/localization.jsonl"


def prune_runs(project_path: Path, keep: int = 3) -> list[str]:
    """Delete all but the *keep* newest ``.raccoon/runs/<run_id>/`` dirs.

    Runs accumulate unbounded otherwise; on the Pi the raw ``sensors.mcap``
    recordings would fill the SD card. Called at run start (Pi-side) so the
    just-created run counts as the newest and is always retained. Only
    directories whose name matches the ``run_id`` timestamp pattern are
    touched, and each is confirmed to resolve inside the runs root before
    removal (defence in depth against traversal). Best-effort: returns the
    list of removed run ids and never raises on individual failures.
    """
    runs_root = project_path / ".raccoon" / "runs"
    if not runs_root.is_dir():
        return []
    runs_resolved = runs_root.resolve()
    run_dirs = [
        d for d in runs_root.iterdir()
        if d.is_dir() and _RUN_ID_RE.match(d.name)
    ]
    # Newest first — run ids are lexically sortable UTC timestamps.
    run_dirs.sort(key=lambda d: d.name, reverse=True)

    removed: list[str] = []
    for d in run_dirs[keep:]:
        if runs_resolved not in d.resolve().parents:
            continue
        try:
            shutil.rmtree(d)
            removed.append(d.name)
        except OSError:
            continue
    return removed


def build_recording_env(
    project_path: Path,
    run_id: str,
    record_hz: float | None = None,
) -> dict[str, str]:
    """Create the run directory and return env vars enabling localization recording.

    Retained for the Web-IDE mission service; localization-only (the IDE does not
    inject the log-dir/profile vars). New CLI callers should use
    :func:`build_run_env` for the full unified-artifact env.
    """
    recording_path = run_dir_path(project_path, run_id) / "localization.jsonl"
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {
        "LIBSTP_RECORD_LOCALIZATION": "1",
        "LIBSTP_RECORDING_PATH": str(recording_path),
    }
    if record_hz is not None:
        env["LIBSTP_RECORDING_HZ"] = str(record_hz)
    return env


def build_run_env(
    run_id: str,
    *,
    absolute: bool,
    project_path: Optional[Path] = None,
    record_localization: bool = True,
    profile: bool = True,
    record_hz: float | None = None,
    record_sensors: bool = True,
) -> dict[str, str]:
    """Env vars that point raccoon-lib's artifact writers at the run dir.

    The child writes into ``.raccoon/runs/<run_id>/``. Pass *absolute*=True for a
    local child (robust regardless of its cwd) and *absolute*=False for a remote
    run (paths are interpreted in the synced project dir on the Pi). When
    *absolute* is True, *project_path* must be given.

    - ``LIBSTP_LOG_DIR``          → the run dir (C++ logger writes ``libstp.jsonl``)
    - ``LIBSTP_RECORD_LOCALIZATION``/``LIBSTP_RECORDING_PATH``/``LIBSTP_RECORDING_HZ``
      → localization recorder (only when *record_localization*)
    - ``RACCOON_PROFILE``         → step profiler output path (only when *profile*)
    - ``RACCOON_RECORD_SENSORS``  → "1"/"0"; carries the sensor-recording opt-out
      to the Pi's nested ``raccoon run --local`` (which recomputes its own run id,
      so only this flag — not the path — propagates). ``RACCOON_SENSORS_PATH`` is
      the intended output path (informational; the Pi resolves its own run dir).
    """
    if absolute:
        if project_path is None:
            raise ValueError("project_path is required when absolute=True")
        run_dir = str(run_dir_path(project_path, run_id))
        loc_path = str(run_dir_path(project_path, run_id) / "localization.jsonl")
        prof_path = str(run_dir_path(project_path, run_id) / "profile.json")
        sensors_path = str(run_dir_path(project_path, run_id) / SENSORS_FILENAME)
    else:
        run_dir = run_rel_dir(run_id)
        loc_path = recording_rel_path(run_id)
        prof_path = f"{run_rel_dir(run_id)}/profile.json"
        sensors_path = f"{run_rel_dir(run_id)}/{SENSORS_FILENAME}"

    env: dict[str, str] = {"LIBSTP_LOG_DIR": run_dir}
    if record_localization:
        env["LIBSTP_RECORD_LOCALIZATION"] = "1"
        env["LIBSTP_RECORDING_PATH"] = loc_path
        if record_hz is not None:
            env["LIBSTP_RECORDING_HZ"] = str(record_hz)
    if profile:
        env["RACCOON_PROFILE"] = prof_path
    env["RACCOON_RECORD_SENSORS"] = "1" if record_sensors else "0"
    env["RACCOON_SENSORS_PATH"] = sensors_path
    return env


def write_run_manifest(
    project_path: Path,
    run_id: str,
    *,
    missions: Optional[list[str]] = None,
    args: Optional[list[str]] = None,
    record_localization: bool = True,
    profile: bool = True,
    record_sensors: bool = True,
    project: Optional[str] = None,
) -> Path:
    """Create the run dir and write ``run.json`` into it; return the run dir.

    Records *which* artifacts were requested — actual presence is discovered at
    download time.
    """
    run_dir = run_dir_path(project_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    started_utc = _dt.datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=_dt.timezone.utc
    )
    started_local = started_utc.astimezone()
    manifest = {
        "run_id": run_id,
        "started_at_utc": started_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started_at_local": started_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "project": project if project is not None else project_path.name,
        "missions": list(missions or []),
        "args": list(args or []),
        "record_localization": bool(record_localization),
        "profile": bool(profile),
        "record_sensors": bool(record_sensors),
        "artifacts": {
            "log": "libstp.jsonl",
            "localization": "localization.jsonl",
            "profile": "profile.json",
            "sensors": SENSORS_FILENAME,
        },
    }
    (run_dir / "run.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return run_dir
