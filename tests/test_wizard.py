"""Unit tests for the interactive project wizard (raccoon_cli/commands/wizard.py)."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers to build mock questionary answers
# ---------------------------------------------------------------------------

def _mock_ask(value):
    """Return a mock questionary prompt object whose .ask() returns *value*."""
    m = MagicMock()
    m.ask.return_value = value
    return m


# ---------------------------------------------------------------------------
# _MOTOR_PORT_CHOICES and _BUTTON_PORT_CHOICES are defined
# ---------------------------------------------------------------------------

class TestChoiceConstants:
    def test_motor_port_choices_defined(self):
        from raccoon_cli.commands.wizard import _MOTOR_PORT_CHOICES
        assert _MOTOR_PORT_CHOICES == ["0", "1", "2", "3"]

    def test_button_port_choices_defined(self):
        from raccoon_cli.commands.wizard import _BUTTON_PORT_CHOICES
        assert _BUTTON_PORT_CHOICES == [str(i) for i in range(11)]


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

class TestValidators:
    def test_int_validator_accepts_integer(self):
        from raccoon_cli.commands.wizard import _int_validator
        assert _int_validator("42") is True
        assert _int_validator("-5") is True

    def test_int_validator_rejects_float_string(self):
        from raccoon_cli.commands.wizard import _int_validator
        result = _int_validator("3.14")
        assert result != True  # noqa: E712 — may be an error string

    def test_pos_float_validator_accepts(self):
        from raccoon_cli.commands.wizard import _pos_float_validator
        assert _pos_float_validator("1.5") is True
        assert _pos_float_validator("100") is True

    def test_pos_float_validator_rejects_zero(self):
        from raccoon_cli.commands.wizard import _pos_float_validator
        result = _pos_float_validator("0")
        assert result != True  # noqa: E712

    def test_pos_float_validator_rejects_negative(self):
        from raccoon_cli.commands.wizard import _pos_float_validator
        result = _pos_float_validator("-1")
        assert result != True  # noqa: E712

    def test_alpha_validator_accepts_boundary(self):
        from raccoon_cli.commands.wizard import _alpha_validator
        assert _alpha_validator("1.0") is True
        assert _alpha_validator("0.5") is True

    def test_alpha_validator_rejects_zero(self):
        from raccoon_cli.commands.wizard import _alpha_validator
        result = _alpha_validator("0")
        assert result != True  # noqa: E712

    def test_alpha_validator_rejects_above_one(self):
        from raccoon_cli.commands.wizard import _alpha_validator
        result = _alpha_validator("1.1")
        assert result != True  # noqa: E712


# ---------------------------------------------------------------------------
# _ask_project_name
# ---------------------------------------------------------------------------

class TestAskProjectName:
    def test_returns_user_input(self):
        from raccoon_cli.commands.wizard import _ask_project_name
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.text.return_value = _mock_ask("MyBot")
            result = _ask_project_name("OldName")
        assert result == "MyBot"

    def test_falls_back_to_existing_on_empty(self):
        from raccoon_cli.commands.wizard import _ask_project_name
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.text.return_value = _mock_ask("")
            result = _ask_project_name("OldName")
        assert result == "OldName"

    def test_falls_back_to_default_on_empty_no_existing(self):
        from raccoon_cli.commands.wizard import _ask_project_name
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.text.return_value = _mock_ask(None)
            result = _ask_project_name("")
        assert result == "My Raccoon Robot"


# ---------------------------------------------------------------------------
# _ask_drivetrain
# ---------------------------------------------------------------------------

class TestAskDrivetrain:
    def test_returns_mecanum(self):
        from raccoon_cli.commands.wizard import _ask_drivetrain
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask("mecanum")
            result = _ask_drivetrain(None)
        assert result == "mecanum"

    def test_returns_differential(self):
        from raccoon_cli.commands.wizard import _ask_drivetrain
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask("differential")
            result = _ask_drivetrain("mecanum")
        assert result == "differential"

    def test_falls_back_to_default_on_none(self):
        from raccoon_cli.commands.wizard import _ask_drivetrain
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask(None)
            result = _ask_drivetrain(None)
        # default is "mecanum" when existing is invalid
        assert result == "mecanum"


# ---------------------------------------------------------------------------
# _ask_motors
# ---------------------------------------------------------------------------

class TestAskMotors:
    def _patch_questionary(self, port_seq, inv_seq):
        """Return a context manager patching questionary with sequential answers."""
        import itertools

        port_iter = iter(port_seq)
        inv_iter  = iter(inv_seq)

        q = MagicMock()
        q.select.side_effect   = lambda *a, **kw: _mock_ask(next(port_iter, "0"))
        q.confirm.side_effect  = lambda *a, **kw: _mock_ask(next(inv_iter, False))
        return q

    def test_differential_returns_two_motors(self):
        from raccoon_cli.commands.wizard import _ask_motors
        q = self._patch_questionary(["0", "1"], [False, True])
        with patch("raccoon_cli.commands.wizard.questionary", q):
            result = _ask_motors("differential", {})
        assert set(result.keys()) == {"left_motor", "right_motor"}
        assert result["left_motor"]  == (0, False)
        assert result["right_motor"] == (1, True)

    def test_mecanum_returns_four_motors(self):
        from raccoon_cli.commands.wizard import _ask_motors
        q = self._patch_questionary(["0", "1", "2", "3"], [False, True, False, True])
        with patch("raccoon_cli.commands.wizard.questionary", q):
            result = _ask_motors("mecanum", {})
        assert set(result.keys()) == {
            "front_left_motor", "front_right_motor",
            "rear_left_motor",  "rear_right_motor",
        }

    def test_uses_existing_defaults(self):
        from raccoon_cli.commands.wizard import _ask_motors
        existing = {"left_motor": {"port": 2, "inverted": True}}
        captured_defaults = {}

        def capture_select(*args, **kwargs):
            captured_defaults["default"] = kwargs.get("default")
            return _mock_ask(kwargs.get("default", "0"))

        q = MagicMock()
        q.select.side_effect  = capture_select
        q.confirm.side_effect = lambda *a, **kw: _mock_ask(kw.get("default", False))

        with patch("raccoon_cli.commands.wizard.questionary", q):
            result = _ask_motors("differential", existing)

        # existing port=2 should be the default for left_motor
        assert result["left_motor"][0] == 2


# ---------------------------------------------------------------------------
# _ask_button
# ---------------------------------------------------------------------------

class TestAskButton:
    def test_returns_selected_port(self):
        from raccoon_cli.commands.wizard import _ask_button
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask("5")
            result = _ask_button({})
        assert result == 5

    def test_uses_existing_default(self):
        from raccoon_cli.commands.wizard import _ask_button
        existing = {"button": {"port": 7}}
        captured = {}

        def capture(*args, **kwargs):
            captured["default"] = kwargs.get("default")
            return _mock_ask(kwargs["default"])

        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.side_effect = capture
            _ask_button(existing)

        assert captured["default"] == "7"


# ---------------------------------------------------------------------------
# _ask_measurements
# ---------------------------------------------------------------------------

class TestAskMeasurements:
    def test_returns_expected_keys(self):
        from raccoon_cli.commands.wizard import _ask_measurements
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.text.return_value = _mock_ask("75.0")
            result = _ask_measurements({})
        assert set(result.keys()) == {
            "wheel_diameter_mm", "track_width_cm", "wheelbase_cm", "vel_filter_alpha"
        }

    def test_values_are_floats(self):
        from raccoon_cli.commands.wizard import _ask_measurements
        answers = iter(["80.0", "22.0", "16.0", "0.9"])
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.text.side_effect = lambda *a, **kw: _mock_ask(next(answers))
            result = _ask_measurements({})
        assert result["wheel_diameter_mm"] == pytest.approx(80.0)
        assert result["track_width_cm"]    == pytest.approx(22.0)
        assert result["wheelbase_cm"]      == pytest.approx(16.0)
        assert result["vel_filter_alpha"]  == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# _ask_ticks
# ---------------------------------------------------------------------------

class TestAskTicks:
    def test_skip_returns_defaults(self):
        from raccoon_cli.commands.wizard import _ask_ticks
        motors = {"left_motor": (0, False), "right_motor": (1, True)}
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask("skip")
            result = _ask_ticks(motors, {}, False)
        assert result == {"left_motor": 1536, "right_motor": 1536}

    def test_manual_entry(self):
        from raccoon_cli.commands.wizard import _ask_ticks
        motors = {"left_motor": (0, False), "right_motor": (1, True)}
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask("manual")
            q.text.return_value   = _mock_ask("2000")
            result = _ask_ticks(motors, {}, False)
        assert result == {"left_motor": 2000, "right_motor": 2000}

    def test_uses_existing_ticks_from_calibration(self):
        from raccoon_cli.commands.wizard import _ask_ticks
        ticks_per_rev = 1024
        ticks_to_rad = (2 * math.pi) / ticks_per_rev
        existing = {"left_motor": {"calibration": {"ticks_to_rad": ticks_to_rad}}}
        motors = {"left_motor": (0, False)}
        with patch("raccoon_cli.commands.wizard.questionary") as q:
            q.select.return_value = _mock_ask("skip")
            result = _ask_ticks(motors, existing, False)
        assert result["left_motor"] == pytest.approx(ticks_per_rev, abs=1)


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

class TestBuildMotorDef:
    def test_structure(self):
        from raccoon_cli.commands.wizard import _build_motor_def
        d = _build_motor_def(port=2, inverted=True, ticks_to_rad=0.004, vel_lpf_alpha=0.8)
        assert d["type"] == "Motor"
        assert d["port"] == 2
        assert d["inverted"] is True
        assert "calibration" in d
        assert d["calibration"]["ticks_to_rad"] == pytest.approx(0.004)
        assert d["calibration"]["vel_lpf_alpha"] == pytest.approx(0.8)


class TestBuildDefinitions:
    def test_all_keys_present(self):
        from raccoon_cli.commands.wizard import _build_definitions
        motors = {"left_motor": (0, False), "right_motor": (1, True)}
        t2r = {"left_motor": 0.004, "right_motor": 0.004}
        defs = _build_definitions(motors, button_port=10, ticks_to_rad=t2r, vel_lpf_alpha=0.8)
        assert "imu" in defs
        assert "left_motor" in defs
        assert "right_motor" in defs
        assert defs["button"]["port"] == 10


class TestBuildKinematics:
    def test_differential_keys(self):
        from raccoon_cli.commands.wizard import _build_kinematics
        motors = {"left_motor": (0, False), "right_motor": (1, True)}
        m = {"wheel_diameter_mm": 75.0, "track_width_cm": 20.0, "wheelbase_cm": 15.0}
        kin = _build_kinematics("differential", motors, m)
        assert kin["type"] == "differential"
        assert "left_motor" in kin
        assert "right_motor" in kin
        assert "wheelbase" not in kin
        assert kin["wheel_radius"] == pytest.approx(0.0375, rel=1e-3)

    def test_mecanum_keys(self):
        from raccoon_cli.commands.wizard import _build_kinematics
        motors = {
            "front_left_motor": (0, False), "front_right_motor": (1, True),
            "rear_left_motor":  (2, False), "rear_right_motor":  (3, True),
        }
        m = {"wheel_diameter_mm": 75.0, "track_width_cm": 20.0, "wheelbase_cm": 15.0}
        kin = _build_kinematics("mecanum", motors, m)
        assert kin["type"] == "mecanum"
        assert "wheelbase" in kin


# ---------------------------------------------------------------------------
# Full wizard command integration (all prompts mocked)
# ---------------------------------------------------------------------------

class TestWizardCommand:
    """Drive the full wizard_command through Click's test runner."""

    def _run_wizard(self, tmp_path: Path, questionary_mock, extra_cli_args=None):
        from click.testing import CliRunner
        from raccoon_cli.commands.wizard import wizard_command
        from rich.console import Console

        (tmp_path / "raccoon.project.yml").write_text(
            "name: TestBot\nuuid: test-uuid-1234\n"
        )

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("raccoon_cli.commands.wizard.questionary", questionary_mock), \
                 patch("raccoon_cli.commands.wizard.require_project", return_value=tmp_path), \
                 patch("raccoon_cli.commands.wizard.load_project_config",
                       return_value={"name": "TestBot", "uuid": "test-uuid-1234"}), \
                 patch("raccoon_cli.commands.wizard.save_project_keys") as save_mock, \
                 patch("raccoon_cli.commands.wizard._connect_step", return_value=False):

                result = runner.invoke(
                    wizard_command,
                    args=(extra_cli_args or ["--dry-run"]),
                    obj={"console": Console(quiet=True)},
                    catch_exceptions=False,
                )
        return result, save_mock

    def _make_questionary_mock(self, drivetrain="differential", confirm_save=True):
        """Build a questionary mock that answers the full wizard flow."""
        q = MagicMock()

        # Select calls: drivetrain, port×N, ticks_cal, button_port
        # differential → 2 motors (2 port selects), then ticks skip, then button
        select_answers = iter([
            drivetrain,    # drivetrain
            "0",           # left_motor port
            "1",           # right_motor port
            "skip",        # ticks calibration
            "10",          # button port
        ])
        q.select.side_effect = lambda *a, **kw: _mock_ask(next(select_answers, "0"))

        # Confirm calls: inverted×2, confirm_save
        confirm_answers = iter([False, True, confirm_save])
        q.confirm.side_effect = lambda *a, **kw: _mock_ask(next(confirm_answers, False))

        # Text calls: project_name, wheel_diam, track, wb, alpha
        text_answers = iter(["TestBot", "75.0", "20.0", "15.0", "0.8"])
        q.text.side_effect = lambda *a, **kw: _mock_ask(next(text_answers, "75.0"))

        return q

    def test_dry_run_exits_cleanly(self, tmp_path):
        q = self._make_questionary_mock()
        result, save_mock = self._run_wizard(tmp_path, q)
        assert result.exit_code == 0, result.output
        save_mock.assert_not_called()

    def test_mecanum_dry_run(self, tmp_path):
        select_answers = iter([
            "mecanum",  # drivetrain
            "0", "1", "2", "3",  # 4 motor ports
            "skip",  # ticks
            "10",    # button
        ])
        q = MagicMock()
        q.select.side_effect = lambda *a, **kw: _mock_ask(next(select_answers, "0"))

        confirm_answers = iter([False, True, False, True])  # inverted×4
        q.confirm.side_effect = lambda *a, **kw: _mock_ask(next(confirm_answers, False))

        text_answers = iter(["TestBot", "75.0", "20.0", "15.0", "0.8"])
        q.text.side_effect = lambda *a, **kw: _mock_ask(next(text_answers, "75.0"))

        result, save_mock = self._run_wizard(tmp_path, q)
        assert result.exit_code == 0, result.output

    def test_save_writes_config(self, tmp_path):
        from raccoon_cli.commands.wizard import wizard_command, save_project_keys
        from raccoon_cli.commands.wizard import (
            _ask_project_name, _ask_drivetrain, _ask_motors,
            _ask_button, _ask_measurements, _ask_ticks,
        )
        q = self._make_questionary_mock(confirm_save=True)

        from click.testing import CliRunner
        from rich.console import Console

        runner = CliRunner()

        (tmp_path / "raccoon.project.yml").write_text(
            "name: TestBot\nuuid: test-uuid-1234\n"
        )

        with patch("raccoon_cli.commands.wizard.questionary", q), \
             patch("raccoon_cli.commands.wizard.require_project", return_value=tmp_path), \
             patch("raccoon_cli.commands.wizard.load_project_config",
                   return_value={"name": "TestBot", "uuid": "test-uuid-1234"}), \
             patch("raccoon_cli.commands.wizard.save_project_keys") as save_mock, \
             patch("raccoon_cli.commands.wizard._connect_step", return_value=False):

            result = runner.invoke(
                wizard_command,
                args=[],          # no --dry-run, so confirm prompt runs
                obj={"console": Console(quiet=True)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        save_mock.assert_called_once()
        saved_args = save_mock.call_args[0][1]
        assert saved_args["name"] == "TestBot"
        assert "robot" in saved_args
        assert "definitions" in saved_args
