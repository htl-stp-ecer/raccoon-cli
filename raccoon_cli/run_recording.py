from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path


def make_run_id() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_recording_env(
    project_path: Path,
    run_id: str,
    record_hz: float | None = None,
) -> dict[str, str]:
    """Create the run directory and return env vars needed to enable recording."""
    recording_path = project_path / ".raccoon" / "runs" / run_id / "localization.jsonl"
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {
        "LIBSTP_RECORD_LOCALIZATION": "1",
        "LIBSTP_RECORDING_PATH": str(recording_path),
    }
    if record_hz is not None:
        env["LIBSTP_RECORDING_HZ"] = str(record_hz)
    return env


def recording_rel_path(run_id: str) -> str:
    return f".raccoon/runs/{run_id}/localization.jsonl"
