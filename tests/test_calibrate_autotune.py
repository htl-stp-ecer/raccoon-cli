"""Autotune: the script running on the robot."""

import subprocess
import sys

import pytest

from raccoon_cli.commands import calibrate

# Stand-ins for the robot library; start() runs the gate the way GenericRobot does.
_FAKE_ROBOT = """
import asyncio

class SetupMission:
    def __init__(self, custom): self.custom = custom
    async def pre_start_gate(self, robot): print("setup gate waits")

class Robot:
    shutdown_in = 120
    setup_mission = SETUP

    async def _pre_start_gate(self): print("robot gate waits")

    def start(self):
        setup = self.setup_mission
        if setup is not None and setup.custom:
            asyncio.run(setup.pre_start_gate(self))
        else:
            asyncio.run(self._pre_start_gate())
        print("missions", [type(m).__name__ for m in self.missions])
        print("shutdown_in", self.shutdown_in)
"""


@pytest.mark.parametrize(
    "setup", ["None", "SetupMission(custom=False)", "SetupMission(custom=True)"]
)
def test_autotune_starts_without_gate_and_time_limit(tmp_path, setup):
    for name, content in {
        "raccoon/__init__.py": (
            "Sequential = object\nclass Mission: pass\ndef auto_tune(): pass\n"
        ),
        "src/__init__.py": "",
        "src/hardware/__init__.py": "",
        "src/hardware/robot.py": _FAKE_ROBOT.replace("SETUP", setup),
        "script.py": calibrate._AUTOTUNE_SCRIPT,
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    result = subprocess.run(
        [sys.executable, "script.py"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "missions ['_AutotuneMission']",
        "shutdown_in 99999",
    ]
