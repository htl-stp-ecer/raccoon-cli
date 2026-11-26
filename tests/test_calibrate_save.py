"""Calibration results are written to config/motors.yml and config/servos.yml."""

from unittest.mock import MagicMock, patch

import pytest

from raccoon_cli.commands import calibrate
from raccoon_cli.project import load_project_config

_HARDWARE = "_motors: !include-merge 'motors.yml'\n_servos: !include-merge 'servos.yml'\n"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "raccoon.project.yml").write_text(
        "name: t\nuuid: u\ndefinitions: !include config/hardware.yml\n"
    )
    (tmp_path / "config/hardware.yml").write_text(_HARDWARE)
    (tmp_path / "config/motors.yml").write_text(
        "# drive motors\nleft:\n  type: Motor\n  port: 0\n  calibration:\n"
        "    ticks_to_rad: 1.0\n    vel_lpf_alpha: 0.05\nright:\n  type: Motor\n  port: 1\n"
    )
    (tmp_path / "config/servos.yml").write_text(
        "# arm\narm:\n  type: Servo\n  port: 0\n  positions:\n    up: 10\n"
    )
    return tmp_path


def test_ticks_are_saved_to_motors_yml(project):
    config = load_project_config(project)

    with patch.object(calibrate.click, "confirm", return_value=True), \
            patch.object(calibrate, "_calibrate_single_motor", return_value=1000.0):
        calibrate._calibrate_ticks(MagicMock(), project, config, local=True)

    assert (project / "config/hardware.yml").read_text() == _HARDWARE
    assert "# drive motors" in (project / "config/motors.yml").read_text()
    definitions = load_project_config(project)["definitions"]
    assert definitions["left"]["calibration"] == {
        "ticks_to_rad": 0.0062832,
        "vel_lpf_alpha": 0.05,
    }
    assert definitions["right"]["calibration"] == {"ticks_to_rad": 0.0062832}


def test_servo_offset_is_saved_to_servos_yml(project):
    path = calibrate._save_servo_offset(project, "arm", 2.5)

    assert path == project / "config/servos.yml"
    assert "# arm" in path.read_text()
    assert (project / "config/hardware.yml").read_text() == _HARDWARE
    assert load_project_config(project)["definitions"]["arm"]["offset"] == 2.5


def test_unknown_entry_is_rejected_without_writing(project):
    before = (project / "config/servos.yml").read_text()

    with pytest.raises(KeyError, match="gripper"):
        calibrate._save_servo_offset(project, "gripper", 1.0)

    assert (project / "config/servos.yml").read_text() == before


def test_first_encoder_read_is_discarded():
    # The first read returns 0 because no value has arrived yet
    readings = iter([0, 50000, 51600, 51600, 53200, 53200, 54800])

    with patch.object(calibrate.click, "prompt"), patch.object(calibrate.time, "sleep"):
        average = calibrate._calibrate_single_motor(
            MagicMock(), "left", 0, False, lambda *_: next(readings)
        )

    assert average == 1600
