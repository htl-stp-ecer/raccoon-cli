"""Robot send-side command trace: boolean flag env + bundle inclusion.

Guards the change from a path-based RACCOON_CMD_TRACE to a boolean flag whose
output (cmd_trace.robot.jsonl) lands in the run dir so `raccoon logs` downloads
it with the rest of the bundle.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from raccoon_cli.run_recording import build_run_env
from raccoon_cli.logs import discover_log_files, load_run_by_index
from raccoon_cli.server.routes import logs as server_logs


def test_build_run_env_enables_cmd_trace_flag_by_default():
    env = build_run_env("20260708T120000Z", absolute=False)
    # Boolean flag, NOT a path — raccoon-lib resolves the file under LIBSTP_LOG_DIR.
    assert env["RACCOON_CMD_TRACE"] == "1"
    assert env["LIBSTP_LOG_DIR"] == ".raccoon/runs/20260708T120000Z"


def test_build_run_env_cmd_trace_opt_out():
    env = build_run_env("20260708T120000Z", absolute=False, cmd_trace=False)
    assert env["RACCOON_CMD_TRACE"] == "0"


def _empty_cmd_trace() -> dict:
    return {
        "path": "/nope/cmd_trace.jsonl", "available": False,
        "total_lines": 0, "matched_lines": 0,
        "window_start_us": 0, "window_end_us": 1, "pad_secs": 2.0, "entries": [],
    }


def test_robot_cmd_trace_file_is_bundled(tmp_path: Path):
    run_id = "20260708T120000Z"
    run_dir = tmp_path / ".raccoon" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "libstp.jsonl").write_text(
        json.dumps({"t": "2026-07-08T12:00:00.000", "elapsed": 0.0, "level": "info",
                    "file": "/x/api.py", "line": 1, "func": "f", "msg": "start"}) + "\n"
    )
    (run_dir / "run.json").write_text(json.dumps({"run_id": run_id}) + "\n")
    # The robot writes its send-side trace here; the bundle must carry it.
    (run_dir / "cmd_trace.robot.jsonl").write_text(
        json.dumps({"t_ns": 1, "seq": 0, "ts_us": 123, "ch": "raccoon/motor/3/velocity_cmd",
                    "kind": "motor_vel", "port": 3, "v": [-1300]}) + "\n"
    )

    files = discover_log_files(tmp_path / ".raccoon" / "runs")
    run = load_run_by_index(files, 1)
    assert run is not None and run.run_dir

    zip_bytes = server_logs._build_run_bundle_zip(
        run, Path(run.run_dir), _empty_cmd_trace(), []
    )

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        robot_line = json.loads(zf.read("cmd_trace.robot.jsonl").decode().strip())

    # File is in the zip and recorded in the manifest artifact inventory.
    assert "cmd_trace.robot.jsonl" in names
    artifacts = {a["name"]: a for a in manifest["artifacts"]}
    assert artifacts["cmd_trace.robot.jsonl"]["present"] is True
    # The reader's receive-side trace name is distinct and does not collide.
    assert "cmd_trace.jsonl" in names
    assert robot_line["kind"] == "motor_vel" and robot_line["port"] == 3
