from __future__ import annotations

from typing import Any, Dict, List


def _arg(name: str, type_name: str = "Any", optional: bool = True, default: Any | None = None) -> Dict[str, Any]:
    return {
        "name": name,
        "type": type_name,
        "optional": optional,
        "default": default,
    }


DEFAULT_LIBRARY_STEPS: List[Dict[str, Any]] = [
    {
        "name": "drive_forward",
        "import": "libstp.step.drive.drive_forward",
        "file": "libstp/step/drive.py",
        "arguments": [_arg("cm", "float"), _arg("velocity", "float")],
    },
    {
        "name": "drive_backward",
        "import": "libstp.step.drive.drive_backward",
        "file": "libstp/step/drive.py",
        "arguments": [_arg("cm", "float"), _arg("velocity", "float")],
    },
    {
        "name": "turn_cw",
        "import": "libstp.step.drive.turn_cw",
        "file": "libstp/step/drive.py",
        "arguments": [_arg("deg", "float"), _arg("omega", "float")],
    },
    {
        "name": "turn_ccw",
        "import": "libstp.step.drive.turn_ccw",
        "file": "libstp/step/drive.py",
        "arguments": [_arg("deg", "float"), _arg("omega", "float")],
    },
    {
        "name": "strafe_left",
        "import": "libstp.step.drive.strafe_left",
        "file": "libstp/step/drive.py",
        "arguments": [_arg("cm", "float"), _arg("velocity", "float")],
    },
    {
        "name": "strafe_right",
        "import": "libstp.step.drive.strafe_right",
        "file": "libstp/step/drive.py",
        "arguments": [_arg("cm", "float"), _arg("velocity", "float")],
    },
    {
        "name": "parallel",
        "import": "libstp.step.parallel",
        "file": "libstp/step/__init__.py",
        "arguments": [_arg("steps", "List[Step]", optional=True)],
    },
    {
        "name": "timeout",
        "import": "libstp.step.timeout",
        "file": "libstp/step/__init__.py",
        "arguments": [_arg("seconds", "float", optional=False)],
    },
    {
        "name": "wait",
        "import": "libstp.step.wait",
        "file": "libstp/step/__init__.py",
        "arguments": [_arg("seconds", "float", optional=False)],
    },
    {
        "name": "custom_step",
        "import": "libstp.step.custom.custom_step",
        "file": "libstp/step/custom.py",
        "arguments": [_arg("callback", "Callable", optional=False)],
    },
    {
        "name": "motor",
        "import": "libstp.step.motor.motor",
        "file": "libstp/step/motor.py",
        "arguments": [_arg("motor_id", "str", optional=False), _arg("power", "float", optional=False)],
    },
    {
        "name": "servo",
        "import": "libstp.step.servo",
        "file": "libstp/step/__init__.py",
        "arguments": [_arg("servo_id", "str", optional=False), _arg("angle", "float", optional=False)],
    },
    {
        "name": "slow_servo",
        "import": "libstp.step.slow_servo",
        "file": "libstp/step/__init__.py",
        "arguments": [_arg("servo_id", "str", optional=False), _arg("angle", "float", optional=False)],
    },
    {
        "name": "backward_lineup_on_white",
        "import": "libstp.step.lineup.backward_lineup_on_white",
        "file": "libstp/step/lineup.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "forward_lineup_on_white",
        "import": "libstp.step.lineup.forward_lineup_on_white",
        "file": "libstp/step/lineup.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "backward_lineup_on_black",
        "import": "libstp.step.lineup.backward_lineup_on_black",
        "file": "libstp/step/lineup.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "forward_lineup_on_black",
        "import": "libstp.step.lineup.forward_lineup_on_black",
        "file": "libstp/step/lineup.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "lineup",
        "import": "libstp.step.lineup.lineup",
        "file": "libstp/step/lineup.py",
        "arguments": [_arg("mode", "str", optional=False)],
    },
    {
        "name": "drive_until_white",
        "import": "libstp.step.drive_until.drive_until_white",
        "file": "libstp/step/drive_until.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "drive_until_black",
        "import": "libstp.step.drive_until.drive_until_black",
        "file": "libstp/step/drive_until.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "follow_line",
        "import": "libstp.step.line_follow.follow_line",
        "file": "libstp/step/line_follow.py",
        "arguments": [_arg("cm", "float"), _arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "follow_line_single",
        "import": "libstp.step.single_line_follow.follow_line_single",
        "file": "libstp/step/single_line_follow.py",
        "arguments": [_arg("power", "float"), _arg("timeout", "float")],
    },
    {
        "name": "wait_for_checkpoint",
        "import": "libstp.step.wait_for_checkpoint",
        "file": "libstp/step/__init__.py",
        "arguments": [_arg("name", "str", optional=False)],
    },
    {
        "name": "breakpoint",
        "import": "libstp_helpers.api.steps.debug.breakpoint",
        "file": "libstp_helpers/api/steps/debug.py",
        "arguments": [_arg("label", "str")],
    },
    {
        "name": "read_sensor",
        "import": "libstp.step.read_light",
        "file": "libstp/step/read_light.py",
        "arguments": []
    }
]
