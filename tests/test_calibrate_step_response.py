"""Step response: the script running on the robot and the remote execution."""

import csv
import os
import signal
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from raccoon_cli.commands import calibrate

# Minimal stand-ins for the robot library: UI calls and motor calls are logged.
_FAKE_HAL = """
import time

class Motor:
    first_read = None
    def __init__(self, port): self.port = port
    def set_speed(self, v): print("drive", self.port, v, flush=True)
    def set_velocity(self, v): print("drive", self.port, v, flush=True)
    def brake(self): print("brake", self.port, flush=True)
    def off(self): print("off", self.port, flush=True)
    def _value(self, value):
        # Like the real HAL: 0 until a value has arrived after the first read
        Motor.first_read = Motor.first_read or time.monotonic()
        return value if time.monotonic() - Motor.first_read > 0.2 else 0
    def get_bemf(self): return self._value(1)
    def get_position(self): return self._value(2)
"""

_FAKE_UI = """
import asyncio, os

class ProgressScreen:
    def __init__(self, message): self.message = message

class UIStep:
    async def run_step(self, robot): await self._execute_step(robot)
    async def wait_for_button(self, text, title):
        print("wait_for_button", flush=True)
        if os.environ.get("FAKE_BUTTON") == "never":
            await asyncio.sleep(60)
    async def run_with_ui(self, screen, task):
        print("screen", screen.message, flush=True)
        await task
"""


def _write_script(root):
    for name, content in {
        "raccoon/__init__.py": "",
        "raccoon/hal.py": _FAKE_HAL,
        "raccoon/ui.py": _FAKE_UI,
        "src/__init__.py": "",
        "src/hardware/__init__.py": "",
        "src/hardware/robot.py": "class Robot: pass\n",
        "script.py": calibrate._STEP_RESPONSE_SCRIPT,
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return [
        sys.executable, "script.py", "--ports", "0", "1",
        "--duration", "0.1", "--brake-tail", "0.1", "--out", "out.csv",
    ]


def test_script_waits_for_button_then_records_behind_spinner(tmp_path):
    result = subprocess.run(
        _write_script(tmp_path), cwd=tmp_path, capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr
    out = result.stdout
    order = [
        out.index("wait_for_button"),
        out.index("screen Measuring step response..."),
        out.index("drive 0 70"),
        out.index("brake 0"),
        out.index("off 0"),
    ]
    assert order == sorted(order)
    with (tmp_path / "out.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert rows and rows[-1]["brake_elapsed_s"] and rows[-1]["bemf_1"] == "1"
    assert all(row["pos_0"] == "2" and row["pos_1"] == "2" for row in rows)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_script_cancelled_while_waiting_never_drives(tmp_path):
    proc = subprocess.Popen(
        _write_script(tmp_path),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "FAKE_BUTTON": "never"},
    )
    try:
        assert proc.stdout.readline().strip() == "wait_for_button"
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        proc.kill()

    assert proc.returncode == 130
    assert "drive" not in out
    assert not (tmp_path / "out.csv").exists()


def test_remote_run_pushes_calibrates_and_plots_locally(tmp_path):
    (tmp_path / "step_response.csv").write_text("stale")
    seen = {}

    async def _remote(console, root, config, args, label):
        seen["args"] = args
        assert not (root / "step_response.csv").exists()
        (root / "step_response.csv").write_text("fresh")

    def _prepare(ctx, local):
        ctx.obj.update(console=MagicMock(), project_root=tmp_path, config={}, local=local)

    with patch.object(calibrate, "_prepare_calibration_context", _prepare), \
            patch.object(calibrate, "_calibrate_remote", AsyncMock(side_effect=_remote)), \
            patch.object(calibrate, "_plot_step_response") as plot:
        result = CliRunner().invoke(
            calibrate.calibrate_group, ["step-response", "--ports", "0, 1"], obj={}
        )

    assert result.exit_code == 0, result.output
    plot.assert_called_once()
    assert seen["args"][:3] == ["step-response", "--ports", "0,1"]
    assert "--no-plot" in seen["args"]

    # The Pi server's command line for these arguments is accepted by the CLI
    with patch.object(calibrate, "_prepare_calibration_context", _prepare), \
            patch.object(calibrate, "_run_step_response_local") as local_run:
        result = CliRunner().invoke(
            calibrate.calibrate_group, ["--local", *seen["args"]], obj={}
        )

    assert result.exit_code == 0, result.output
    local_run.assert_called_once()


@pytest.mark.parametrize("out", ["/tmp/out.csv", "../out.csv"])
def test_remote_run_rejects_output_outside_the_project(tmp_path, out):
    with patch.object(calibrate, "_calibrate_remote", AsyncMock()) as remote, \
            pytest.raises(SystemExit):
        calibrate._run_step_response_remote(
            MagicMock(), tmp_path, {}, "0", "speed", 70, 1.0, 1.0, 100, out
        )
    remote.assert_not_called()
