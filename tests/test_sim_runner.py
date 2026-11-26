import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from raccoon_cli.ide.sim.runner import (
    _build_sim_robot_config,
    _install_mock_transport_compatibility,
)


def test_sim_uses_generated_robot_motor_wiring_and_sensor_geometry(monkeypatch):
    sim = ModuleType("raccoon.testing.sim")
    sim.SimRobotConfig = lambda: SimpleNamespace(
        width_cm=18.0, length_cm=22.0, line_sensors=[]
    )
    sim.LineSensorMount = SimpleNamespace
    monkeypatch.setitem(sys.modules, sim.__name__, sim)

    robot = _build_sim_robot_config({
        "robot": {
            "drive": {"kinematics": {"left_motor": "left", "right_motor": "right"}},
            "physical": {
                "rotation_center": {"x_cm": 9, "y_cm": 7},
                "sensors": [{"name": "ir", "x_cm": 6, "y_cm": 13}],
            },
        },
        "definitions": {
            "left": {"port": 2, "inverted": False},
            "right": {"port": 3, "inverted": True},
            "ir": {"type": "IRSensor", "port": 5},
        },
    })

    assert (robot.left_motor_port, robot.right_motor_port) == (2, 3)
    assert robot.left_motor_inverted is False
    assert robot.right_motor_inverted is True
    assert (robot.rotation_center_forward_cm, robot.rotation_center_strafe_cm) == (-4, 0)
    assert robot.line_sensors == [
        SimpleNamespace(name="ir", analog_port=5, forward_cm=2, strafe_cm=3)
    ]


@pytest.mark.parametrize("transport_available", [True, False])
def test_sim_only_adapts_transportless_wheels(monkeypatch, transport_available):
    ui = ModuleType("raccoon.ui")
    ui.step = ModuleType("raccoon.ui.step")
    get_transport = Mock()
    if not transport_available:
        get_transport.side_effect = RuntimeError(
            "raccoon transport is not available in this build (mock driver bundle)"
        )
    ui.step.get_transport = get_transport
    monkeypatch.setitem(sys.modules, ui.__name__, ui)
    monkeypatch.setitem(sys.modules, ui.step.__name__, ui.step)

    speed_mode = ModuleType("raccoon.step.motion.set_speed_mode")

    class SetSpeedMode:
        _enabled = True

        async def _execute_step(self, robot):
            raise AssertionError("The simulator tried to contact the firmware")

    original_execute = SetSpeedMode._execute_step
    speed_mode.SetSpeedMode = SetSpeedMode
    speed_mode.set_speed_mode_enabled = Mock()
    monkeypatch.setitem(sys.modules, speed_mode.__name__, speed_mode)

    _install_mock_transport_compatibility()

    if transport_available:
        assert ui.step.get_transport is get_transport
        assert SetSpeedMode._execute_step is original_execute
    else:
        screen_transport = ui.step.get_transport()
        with pytest.raises(RuntimeError, match="requires the robot screen"):
            screen_transport.publish_raw("raccoon/screen_render", b"")
        asyncio.run(SetSpeedMode()._execute_step(None))
        speed_mode.set_speed_mode_enabled.assert_called_once_with(True)
