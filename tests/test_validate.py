"""Tests for raccoon_cli/validate.py — project consistency validation.

All tests use real temporary directories (no mocks) so that file-system
interactions are caught exactly as they would be at runtime.
"""

from __future__ import annotations

from pathlib import Path

import logging

import json

import pytest
import yaml

from raccoon_cli import validation
from raccoon_cli.validation import (
    Severity,
    ValidationResult,
    class_name_to_expected_file,
    file_name_to_expected_class,
    validate_project,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(
    tmp_path: Path,
    missions_in_config: list,
    mission_files: list[str] | None = None,
    main_py_imports: list[str] | None = None,
) -> Path:
    """
    Scaffold a minimal project under tmp_path.

    Args:
        missions_in_config: list of entries for raccoon.project.yml ``missions``
            key (strings or dicts).
        mission_files: filenames to create under src/missions/ (empty files).
        main_py_imports: list of ``from .missions.<x> import <Y>`` lines for main.py.
    """
    (tmp_path / "src" / "missions").mkdir(parents=True)

    # raccoon.project.yml
    config = {
        "name": "TestProject",
        "uuid": "test-uuid",
        "format_version": 2,
        "missions": missions_in_config,
    }
    (tmp_path / "raccoon.project.yml").write_text(yaml.dump(config), encoding="utf-8")

    # mission files
    for fname in mission_files or []:
        (tmp_path / "src" / "missions" / fname).write_text("# stub\n", encoding="utf-8")

    # src/__init__.py so it looks like a package
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "missions" / "__init__.py").write_text("", encoding="utf-8")

    # main.py
    lines = ["from src.hardware.robot import Robot\n"]
    for imp in main_py_imports or []:
        lines.append(imp + "\n")
    (tmp_path / "src" / "main.py").write_text("".join(lines), encoding="utf-8")

    return tmp_path


@pytest.fixture(autouse=True)
def _stub_generation_check(request, monkeypatch):
    """Most tests use partial configs that real codegen would reject (and run
    without the ``raccoon`` package installed), so skip the in-memory generation
    stage for them. Classes that set ``real_generation = True`` opt out."""
    if getattr(request.cls, "real_generation", False):
        return
    monkeypatch.setattr(validation, "_check_generation", lambda result, config: None)


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


def _severities(result: ValidationResult) -> list[str]:
    return [i.severity.value for i in result.issues]


# ---------------------------------------------------------------------------
# Name-conversion helpers
# ---------------------------------------------------------------------------

class TestClassNameToExpectedFile:
    def test_simple(self):
        assert class_name_to_expected_file("M030HelloMission") == "m030_hello_mission.py"

    def test_multi_word(self):
        assert class_name_to_expected_file("M010DriveDownRampMission") == "m010_drive_down_ramp_mission.py"

    def test_setup(self):
        assert class_name_to_expected_file("M000SetupMission") == "m000_setup_mission.py"

    def test_shutdown(self):
        assert class_name_to_expected_file("M999ShutdownMission") == "m999_shutdown_mission.py"

    def test_lowercase_m_prefix(self):
        assert class_name_to_expected_file("m020CollectConesMission") == "m020_collect_cones_mission.py"

    def test_unparseable_returns_none(self):
        assert class_name_to_expected_file("NotAMission") is None


class TestFileNameToExpectedClass:
    def test_simple(self):
        assert file_name_to_expected_class("m030_hello_mission.py") == "M030HelloMission"

    def test_multi_word(self):
        assert file_name_to_expected_class("m010_drive_down_ramp_mission.py") == "M010DriveDownRampMission"

    def test_setup(self):
        assert file_name_to_expected_class("m000_setup_mission.py") == "M000SetupMission"

    def test_shutdown(self):
        assert file_name_to_expected_class("m999_shutdown_mission.py") == "M999ShutdownMission"

    def test_without_py_extension(self):
        assert file_name_to_expected_class("m030_hello_mission") == "M030HelloMission"

    def test_roundtrip_class_to_file_to_class(self):
        original = "M020CollectConesMission"
        file_name = class_name_to_expected_file(original)
        assert file_name_to_expected_class(file_name) == original

    def test_roundtrip_file_to_class_to_file(self):
        original = "m010_drive_down_ramp_mission.py"
        class_name = file_name_to_expected_class(original)
        assert class_name_to_expected_file(class_name) == original


# ---------------------------------------------------------------------------
# Clean project — no issues
# ---------------------------------------------------------------------------

class TestCleanProject:
    def test_no_missions_no_issues(self, tmp_path):
        project = _make_project(tmp_path, missions_in_config=[])
        result = validate_project(project)
        assert not result.issues

    def test_fully_consistent_project_passes(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M000SetupMission", "M010DriveMission", "M999ShutdownMission"],
            mission_files=["m000_setup_mission.py", "m010_drive_mission.py", "m999_shutdown_mission.py"],
            main_py_imports=[
                "from .missions.m000_setup_mission import M000SetupMission",
                "from .missions.m010_drive_mission import M010DriveMission",
                "from .missions.m999_shutdown_mission import M999ShutdownMission",
            ],
        )
        result = validate_project(project)
        assert not result.issues


# ---------------------------------------------------------------------------
# Drift: config entry has no file
# ---------------------------------------------------------------------------

class TestConfigMissingFile:
    def test_single_missing_file_is_error(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M030HelloMission"],
            mission_files=[],  # file absent
        )
        result = validate_project(project)
        assert result.has_errors
        assert "config_missing_file" in _codes(result)

    def test_error_message_names_the_class_and_file(self, tmp_path):
        project = _make_project(tmp_path, missions_in_config=["M030HelloMission"])
        result = validate_project(project)
        msg = result.errors[0].message
        assert "M030HelloMission" in msg
        assert "m030_hello_mission.py" in msg

    def test_one_present_one_missing_reports_only_missing(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M000SetupMission", "M030HelloMission"],
            mission_files=["m000_setup_mission.py"],  # m030 missing
        )
        result = validate_project(project)
        errors = [i for i in result.errors if i.code == "config_missing_file"]
        assert len(errors) == 1
        assert "M030HelloMission" in errors[0].message

    def test_shutdown_mission_missing_file_is_also_error(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M999ShutdownMission"],
            mission_files=[],
        )
        result = validate_project(project)
        assert result.has_errors


# ---------------------------------------------------------------------------
# Drift: file on disk not in config
# ---------------------------------------------------------------------------

class TestFileNotInConfig:
    def test_orphaned_file_is_warning(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=[],
            mission_files=["m040_hello_mission.py"],
        )
        result = validate_project(project)
        assert not result.has_errors
        assert "file_not_in_config" in _codes(result)

    def test_warning_message_names_file_and_class(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=[],
            mission_files=["m040_hello_mission.py"],
        )
        result = validate_project(project)
        w = next(i for i in result.warnings if i.code == "file_not_in_config")
        assert "m040_hello_mission.py" in w.message
        assert "M040HelloMission" in w.message

    def test_multiple_orphaned_files_each_get_warning(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=[],
            mission_files=["m040_hello_mission.py", "m050_hello_mission.py", "m060_hello_mission.py"],
        )
        result = validate_project(project)
        file_warnings = [i for i in result.issues if i.code == "file_not_in_config"]
        assert len(file_warnings) == 3


# ---------------------------------------------------------------------------
# Drift: main.py imports ghost file (doesn't exist on disk)
# ---------------------------------------------------------------------------

class TestMainImportMissingFile:
    def test_import_of_nonexistent_file_is_error(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M030HelloMission"],
            mission_files=[],  # file absent
            main_py_imports=["from .missions.m030_hello_mission import M030HelloMission"],
        )
        result = validate_project(project)
        assert result.has_errors
        assert "import_missing_file" in _codes(result)

    def test_import_error_message_names_module(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M030HelloMission"],
            mission_files=[],
            main_py_imports=["from .missions.m030_hello_mission import M030HelloMission"],
        )
        result = validate_project(project)
        err = next(i for i in result.errors if i.code == "import_missing_file")
        assert "m030_hello_mission" in err.message

    def test_valid_import_does_not_trigger_error(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M030HelloMission"],
            mission_files=["m030_hello_mission.py"],
            main_py_imports=["from .missions.m030_hello_mission import M030HelloMission"],
        )
        result = validate_project(project)
        assert not result.has_errors


# ---------------------------------------------------------------------------
# Drift: main.py imports class not in config
# ---------------------------------------------------------------------------

class TestMainImportNotInConfig:
    def test_import_not_in_config_is_warning(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=[],
            mission_files=["m040_hello_mission.py"],
            main_py_imports=["from .missions.m040_hello_mission import M040HelloMission"],
        )
        result = validate_project(project)
        assert not result.has_errors
        assert "import_not_in_config" in _codes(result)


# ---------------------------------------------------------------------------
# Regression: exact clawbot scenario
# ---------------------------------------------------------------------------

class TestClawbotRegression:
    """Reproduces the exact drift state found in Ecer2026/clawbot."""

    def _build_clawbot(self, tmp_path: Path) -> Path:
        # Config has M030HelloMission; actual files are m040/m050/m060
        missions_in_config = [
            "M000SetupMission",
            "M010DriveDownRampMission",
            "M020CollectConesMission",
            {"M999ShutdownMission": "shutdown"},
            "M030HelloMission",   # ← ghost: no file exists for this
        ]
        mission_files = [
            "m000_setup_mission.py",
            "m010_drive_down_ramp_mission.py",
            "m020_collect_cones_mission.py",
            "m040_hello_mission.py",  # ← unregistered
            "m050_hello_mission.py",  # ← unregistered
            "m060_hello_mission.py",  # ← unregistered
            "m999_shutdown_mission.py",
        ]
        main_py_imports = [
            "from .missions.m030_hello_mission import M030HelloMission",  # file missing
            "from .missions.m040_hello_mission import M040HelloMission",  # not in config
            "from .missions.m050_hello_mission import M050HelloMission",  # not in config
            "from .missions.m060_hello_mission import M060HelloMission",  # not in config
        ]
        return _make_project(tmp_path, missions_in_config, mission_files, main_py_imports)

    def test_clawbot_state_fails_validation(self, tmp_path):
        project = self._build_clawbot(tmp_path)
        result = validate_project(project)
        assert result.has_errors, "Clawbot drift should produce at least one error"

    def test_clawbot_has_config_missing_file_error(self, tmp_path):
        """M030HelloMission in config but no m030_hello_mission.py → ERROR."""
        project = self._build_clawbot(tmp_path)
        result = validate_project(project)
        errors = [i for i in result.errors if i.code == "config_missing_file"]
        assert any("M030HelloMission" in e.message for e in errors)

    def test_clawbot_has_import_missing_file_error(self, tmp_path):
        """main.py imports m030_hello_mission which doesn't exist → ERROR."""
        project = self._build_clawbot(tmp_path)
        result = validate_project(project)
        errors = [i for i in result.errors if i.code == "import_missing_file"]
        assert any("m030_hello_mission" in e.message for e in errors)

    def test_clawbot_has_unregistered_file_warnings(self, tmp_path):
        """m040/m050/m060 exist but are not in config → WARNING each."""
        project = self._build_clawbot(tmp_path)
        result = validate_project(project)
        file_warnings = [i for i in result.warnings if i.code == "file_not_in_config"]
        unregistered = {w.message for w in file_warnings}
        for stem in ("m040", "m050", "m060"):
            assert any(stem in msg for msg in unregistered), f"{stem} should have file_not_in_config warning"

    def test_clawbot_has_import_not_in_config_warnings(self, tmp_path):
        """m040/m050/m060 imported in main.py but not in config → WARNING each."""
        project = self._build_clawbot(tmp_path)
        result = validate_project(project)
        import_warnings = [i for i in result.warnings if i.code == "import_not_in_config"]
        classes = {w.message for w in import_warnings}
        for cls in ("M040HelloMission", "M050HelloMission", "M060HelloMission"):
            assert any(cls in msg for msg in classes), f"{cls} should have import_not_in_config warning"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_missing_missions_dir_reports_config_errors_only(self, tmp_path):
        """If src/missions/ doesn't exist, config entries still produce errors."""
        config = {
            "name": "TestProject",
            "uuid": "test-uuid",
            "format_version": 2,
            "missions": ["M010DriveMission"],
        }
        (tmp_path / "raccoon.project.yml").write_text(yaml.dump(config), encoding="utf-8")
        # Do NOT create src/missions/
        result = validate_project(tmp_path)
        assert result.has_errors

    def test_no_main_py_does_not_crash(self, tmp_path):
        """Absence of main.py is fine — import checks are skipped."""
        project = _make_project(
            tmp_path,
            missions_in_config=["M000SetupMission"],
            mission_files=["m000_setup_mission.py"],
        )
        (project / "src" / "main.py").unlink()
        result = validate_project(project)
        assert not result.has_errors

    def test_mission_entry_as_dict_with_role(self, tmp_path):
        """Dict-style config entries like {M999ShutdownMission: shutdown} are parsed."""
        project = _make_project(
            tmp_path,
            missions_in_config=[{"M999ShutdownMission": "shutdown"}],
            mission_files=["m999_shutdown_mission.py"],
        )
        result = validate_project(project)
        assert not result.has_errors

    def test_non_mission_imports_in_main_are_ignored(self, tmp_path):
        """Imports not matching the .missions. pattern don't raise spurious errors."""
        project = _make_project(
            tmp_path,
            missions_in_config=[],
            mission_files=[],
            main_py_imports=["from src.hardware.robot import Robot"],
        )
        result = validate_project(project)
        assert not result.issues


# ---------------------------------------------------------------------------
# Defs attribute access — runtime AttributeError prevention
# ---------------------------------------------------------------------------

def _write_config(project: Path, definitions: dict | None = None) -> None:
    config: dict = {"name": "TestProject", "uuid": "test-uuid", "format_version": 2}
    if definitions is not None:
        config["definitions"] = definitions
    (project / "raccoon.project.yml").write_text(yaml.dump(config), encoding="utf-8")


def _write_defs_py(project: Path, attr_names: list[str]) -> None:
    """Write a minimal generated src/hardware/defs.py declaring attr_names."""
    hardware = project / "src" / "hardware"
    hardware.mkdir(parents=True, exist_ok=True)
    lines = ["class Defs:"]
    for name in attr_names:
        lines.append(f"    {name} = object()")
    lines.append("")
    lines.append("defs = Defs()")
    (hardware / "defs.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_mission(project: Path, filename: str, body_lines: list[str]) -> None:
    missions = project / "src" / "missions"
    missions.mkdir(parents=True, exist_ok=True)
    lines = [
        "from raccoon import *",
        "",
        "from src.hardware.defs import Defs",
        "",
        "",
        "class SomeMission(Mission):",
        "    def sequence(self):",
    ]
    lines.extend(f"        {ln}" for ln in body_lines)
    (missions / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestDefsAttributeAccess:
    def test_unknown_defs_attribute_is_error(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor_right"])
        result = validate_project(tmp_path)
        assert result.has_errors
        assert "defs_unknown_attribute" in _codes(result)

    def test_known_defs_attribute_from_config_passes(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor_left"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_known_defs_attribute_from_generated_file_passes(self, tmp_path):
        """Attribute present in generated defs.py but not (yet) in config still ok."""
        _write_config(tmp_path, definitions=None)
        _write_defs_py(tmp_path, ["imu", "arm", "analog_sensors"])
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.arm"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_error_message_names_file_line_and_attr(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.wheel"])
        result = validate_project(tmp_path)
        err = next(i for i in result.errors if i.code == "defs_unknown_attribute")
        assert "Defs.wheel" in err.message
        assert "m010_go_mission.py" in err.message

    def test_close_match_is_suggested(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor_lef"])
        result = validate_project(tmp_path)
        err = next(i for i in result.errors if i.code == "defs_unknown_attribute")
        assert err.hint is not None and "motor_left" in err.hint

    def test_lowercase_defs_instance_is_also_checked(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return defs.ghost"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" in _codes(result)

    def test_dunder_access_is_ignored(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.__class__"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_imu_and_analog_sensors_are_always_valid(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(
            tmp_path,
            "m010_go_mission.py",
            ["x = Defs.imu", "return Defs.analog_sensors"],
        )
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_auto_sensor_pair_group_prefix_is_valid(self, tmp_path):
        """left/right analog pair auto-creates a group attribute — accept it."""
        _write_config(
            tmp_path,
            {
                "tophat_left_line": {"type": "AnalogSensor", "port": 0},
                "tophat_right_line": {"type": "AnalogSensor", "port": 1},
            },
        )
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.tophat"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_files_not_importing_defs_are_not_scanned(self, tmp_path):
        """A file that never imports Defs is skipped even if it has Defs.x text."""
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        missions = tmp_path / "src" / "missions"
        missions.mkdir(parents=True, exist_ok=True)
        # Local class named Defs, no import from a *.defs module.
        (missions / "m010_go_mission.py").write_text(
            "class Defs:\n    pass\n\nx = Defs.anything\n", encoding="utf-8"
        )
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_defs_check_can_be_disabled(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.ghost"])
        result = validate_project(tmp_path, defs_check=False)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_no_config_definitions_and_no_defs_file_skips_check(self, tmp_path):
        """Without any source of truth for Defs, don't guess — no false errors."""
        _write_config(tmp_path, definitions=None)
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.whatever"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)

    def test_generated_hardware_files_are_not_scanned(self, tmp_path):
        """The generated defs.py itself must not be flagged."""
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})
        _write_defs_py(tmp_path, ["motor_left", "imu", "analog_sensors"])
        result = validate_project(tmp_path)
        assert "defs_unknown_attribute" not in _codes(result)


# ---------------------------------------------------------------------------
# Defs access — every way a mission can reach Defs
# ---------------------------------------------------------------------------

def _write_raw_mission(project: Path, filename: str, source: str) -> None:
    missions = project / "src" / "missions"
    missions.mkdir(parents=True, exist_ok=True)
    (missions / filename).write_text(source, encoding="utf-8")


class TestDefsAccessForms:
    def _project(self, tmp_path):
        _write_config(tmp_path, {"motor_left": {"type": "Motor", "port": 0}})

    def test_aliased_class_import_is_checked(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "from src.hardware.defs import Defs as D\nx = D.ghost\n",
        )
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))

    def test_aliased_class_import_valid_name_passes(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "from src.hardware.defs import Defs as D\nx = D.motor_left\n",
        )
        assert "defs_unknown_attribute" not in _codes(validate_project(tmp_path))

    def test_module_import_is_checked(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "from src.hardware import defs\nx = defs.Defs.ghost\n",
        )
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))

    def test_module_import_as_alias_is_checked(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "import src.hardware.defs as d\nx = d.Defs.ghost\n",
        )
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))

    def test_relative_module_import_is_checked(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "from ..hardware import defs as h\nx = h.defs.ghost\n",
        )
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))

    @pytest.mark.parametrize("base", ["robot", "self.robot"])
    def test_robot_defs_is_checked(self, tmp_path, base):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            f"def f(self, robot):\n    return {base}.defs.ghost\n",
        )
        result = validate_project(tmp_path)
        err = next(i for i in result.errors if i.code == "defs_unknown_attribute")
        assert f"{base}.defs.ghost" in err.message

    def test_robot_defs_valid_name_passes(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "def f(robot):\n    return robot.defs.motor_left\n",
        )
        assert "defs_unknown_attribute" not in _codes(validate_project(tmp_path))

    def test_defs_attribute_on_unrelated_object_is_ignored(self, tmp_path):
        self._project(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            "def f(other):\n    return other.defs.ghost\n",
        )
        assert "defs_unknown_attribute" not in _codes(validate_project(tmp_path))

    def test_stale_defs_py_does_not_hide_a_removed_entry(self, tmp_path):
        """motor1 was removed from the YAML but the old defs.py still has it."""
        self._project(tmp_path)
        _write_defs_py(tmp_path, ["motor_left", "motor1"])
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor1"])
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))


# ---------------------------------------------------------------------------
# Generation check — run the real generators in memory
# ---------------------------------------------------------------------------

def _full_config() -> dict:
    """A config the real generators accept."""
    return {
        "name": "TestProject",
        "uuid": "test-uuid",
        "format_version": 2,
        "definitions": {
            "button": {"type": "DigitalSensor", "port": 0},
            "motor_left": {"type": "Motor", "port": 0, "inverted": False},
        },
        "robot": {"motion_pid": {}, "shutdown_in": 120},
    }


def _write_full_config(project: Path, config: dict) -> None:
    (project / "raccoon.project.yml").write_text(yaml.dump(config), encoding="utf-8")


class TestGenerationCheck:
    real_generation = True

    @pytest.fixture(autouse=True)
    def _need_raccoon(self):
        pytest.importorskip("raccoon")

    def _failures(self, result):
        return [i for i in result.errors if i.code == "codegen_failed"]

    def test_valid_config_has_no_generation_errors(self, tmp_path):
        _write_full_config(tmp_path, _full_config())
        result = validate_project(tmp_path)
        assert not self._failures(result)
        assert "codegen_check_unavailable" not in _codes(result)

    def test_missing_button_is_reported_once(self, tmp_path):
        cfg = _full_config()
        del cfg["definitions"]["button"]
        _write_full_config(tmp_path, cfg)
        failures = self._failures(validate_project(tmp_path))
        assert len(failures) == 1  # defs.py and defs.pyi fail identically
        assert "definitions.button" in failures[0].message

    def test_missing_motion_pid_is_reported(self, tmp_path):
        cfg = _full_config()
        del cfg["robot"]["motion_pid"]
        _write_full_config(tmp_path, cfg)
        failures = self._failures(validate_project(tmp_path))
        assert any("motion_pid" in i.message for i in failures)

    def test_missing_shutdown_in_is_reported(self, tmp_path):
        cfg = _full_config()
        del cfg["robot"]["shutdown_in"]
        _write_full_config(tmp_path, cfg)
        failures = self._failures(validate_project(tmp_path))
        assert any("shutdown_in" in i.message for i in failures)

    def test_unknown_hardware_type_is_reported(self, tmp_path):
        cfg = _full_config()
        cfg["definitions"]["gizmo"] = {"type": "NopeType"}
        _write_full_config(tmp_path, cfg)
        failures = self._failures(validate_project(tmp_path))
        assert any("NopeType" in i.message for i in failures)

    def test_invalid_identifier_is_reported(self, tmp_path):
        cfg = _full_config()
        cfg["definitions"]["my-motor"] = {"type": "Motor", "port": 1}
        _write_full_config(tmp_path, cfg)
        failures = self._failures(validate_project(tmp_path))
        assert any("my-motor" in i.message for i in failures)

    def test_defs_names_come_from_generated_source(self, tmp_path):
        _write_full_config(tmp_path, _full_config())
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor_left"])
        assert "defs_unknown_attribute" not in _codes(validate_project(tmp_path))

    def test_unknown_defs_attribute_is_still_flagged(self, tmp_path):
        _write_full_config(tmp_path, _full_config())
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor1"])
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))

    def test_stale_defs_py_does_not_hide_a_removed_entry(self, tmp_path):
        _write_full_config(tmp_path, _full_config())
        _write_defs_py(tmp_path, ["motor_left", "motor1"])
        _write_mission(tmp_path, "m010_go_mission.py", ["return Defs.motor1"])
        assert "defs_unknown_attribute" in _codes(validate_project(tmp_path))

    def test_validation_writes_no_files(self, tmp_path):
        _write_full_config(tmp_path, _full_config())
        validate_project(tmp_path)
        assert not (tmp_path / "src" / "hardware").exists()

    def test_logger_level_is_restored(self, tmp_path):
        _write_full_config(tmp_path, _full_config())
        logger = logging.getLogger("raccoon")
        before = logger.level
        validate_project(tmp_path)
        assert logger.level == before

    def test_check_can_be_disabled(self, tmp_path):
        cfg = _full_config()
        del cfg["definitions"]["button"]
        _write_full_config(tmp_path, cfg)
        result = validate_project(tmp_path, codegen_check=False)
        assert not self._failures(result)


class TestGenerationUnavailable:
    real_generation = True

    def test_missing_raccoon_package_is_a_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validation, "_raccoon_importable", lambda: False)
        _write_full_config(tmp_path, _full_config())
        result = validate_project(tmp_path)
        err = next(i for i in result.errors if i.code == "codegen_check_unavailable")
        assert "raccoon-library" in err.hint

    def test_disabling_the_check_avoids_the_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validation, "_raccoon_importable", lambda: False)
        _write_full_config(tmp_path, _full_config())
        result = validate_project(tmp_path, codegen_check=False)
        assert "codegen_check_unavailable" not in _codes(result)


class TestGeneratedTypeCheck:
    """pyright over the generated defs.py (the pyright call itself is faked)."""

    SOURCE = "x = 1\n"

    def _fake_run(self, monkeypatch, diagnostics, *, found=True):
        import subprocess

        monkeypatch.setattr(
            validation.shutil, "which", lambda name, **kw: "/bin/pyright" if found else None
        )
        out = json.dumps({"generalDiagnostics": diagnostics})
        monkeypatch.setattr(
            validation.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=out, stderr=""),
        )

    def test_type_error_becomes_an_error_with_line(self, monkeypatch):
        self._fake_run(monkeypatch, [{
            "severity": "error",
            "message": 'Argument of type "Literal[\'1ß\']" cannot be assigned\n  detail',
            "range": {"start": {"line": 27, "character": 8}},
        }])
        result = ValidationResult()
        validation._check_generated_types(result, self.SOURCE)
        issue = next(i for i in result.errors if i.code == "defs_type_error")
        assert "line 28" in issue.message
        assert "detail" not in issue.message

    def test_warnings_are_ignored(self, monkeypatch):
        self._fake_run(monkeypatch, [{
            "severity": "warning",
            "message": "Import could not be resolved from source",
            "range": {"start": {"line": 2, "character": 0}},
        }])
        result = ValidationResult()
        validation._check_generated_types(result, self.SOURCE)
        assert not result.issues

    def test_without_pyright_warns_and_skips(self, monkeypatch):
        self._fake_run(monkeypatch, [], found=False)
        result = ValidationResult()
        validation._check_generated_types(result, self.SOURCE)
        assert not result.errors
        warning = next(i for i in result.warnings if i.code == "type_check_skipped")
        assert "uv add pyright" in warning.hint

    def test_unparseable_output_is_not_an_error(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(validation.shutil, "which", lambda name, **kw: "/bin/pyright")
        monkeypatch.setattr(
            validation.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="not json", stderr=""),
        )
        result = ValidationResult()
        validation._check_generated_types(result, self.SOURCE)
        assert not result.issues


# ---------------------------------------------------------------------------
# Auto-validate scope — only `run` is validated automatically
# ---------------------------------------------------------------------------

class TestAutoValidateScope:
    @pytest.mark.parametrize(
        "command, expected",
        [
            ("run", True),
            ("codegen", False),  # validates itself, see commands/codegen.py
            ("validate", False),  # is the validator
            ("missions", False),
            ("shell", False),
            ("sync", False),
            ("calibrate", False),
            ("connect", False),
        ],
    )
    def test_only_run_is_auto_validated(
        self, tmp_path, monkeypatch, command, expected
    ):
        from types import SimpleNamespace

        from raccoon_cli import cli, project

        calls = []
        monkeypatch.setattr(project, "find_project_root", lambda: tmp_path)
        monkeypatch.setattr(
            validation, "run_validation_or_exit",
            lambda console, root, **kw: calls.append(root),
        )
        ctx = SimpleNamespace(
            obj={"console": None, "no_validate": False}, invoked_subcommand=command
        )
        cli._run_auto_validate(ctx)
        assert bool(calls) is expected

# ---------------------------------------------------------------------------
# CLI — raccoon validate exit codes
# ---------------------------------------------------------------------------

class TestValidateCLI:
    def _invoke(self, project_root):
        from click.testing import CliRunner
        from raccoon_cli.cli import main

        runner = CliRunner()
        return runner.invoke(main, ["validate"], catch_exceptions=False,
                             env={"PWD": str(project_root)},
                             # CliRunner changes cwd via mix_stderr/obj; use explicit obj instead
                             obj={"console": __import__("rich.console", fromlist=["Console"]).Console(
                                 highlight=False, markup=False
                             ), "initialized": True, "log_summary": _DummySummary()})

    def test_clean_project_exits_zero(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M000SetupMission"],
            mission_files=["m000_setup_mission.py"],
        )
        from click.testing import CliRunner
        from raccoon_cli.commands.validate import validate_command

        runner = CliRunner()
        with runner.isolated_filesystem():
            import os, shutil
            # copy project into isolated fs so find_project_root works
            for item in project.iterdir():
                dest = Path(os.getcwd()) / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            result = runner.invoke(validate_command, [], catch_exceptions=False,
                                   obj={"console": __import__("rich.console", fromlist=["Console"]).Console()})
        assert result.exit_code == 0

    def test_project_with_errors_exits_nonzero(self, tmp_path):
        project = _make_project(
            tmp_path,
            missions_in_config=["M030HelloMission"],
            mission_files=[],  # missing
        )
        from click.testing import CliRunner
        from raccoon_cli.commands.validate import validate_command
        import os, shutil

        runner = CliRunner()
        with runner.isolated_filesystem():
            for item in project.iterdir():
                dest = Path(os.getcwd()) / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            result = runner.invoke(validate_command, [],
                                   obj={"console": __import__("rich.console", fromlist=["Console"]).Console()})
        assert result.exit_code != 0


class _DummySummary:
    def clear(self): pass
    warnings: list = []
    errors: list = []


# ---------------------------------------------------------------------------
# create mission no longer touches main.py
# ---------------------------------------------------------------------------

class TestCreateMissionDoesNotTouchMainPy:
    def test_main_py_unchanged_after_create(self, tmp_path):
        """raccoon mission create must not insert imports into main.py."""
        import inspect
        from raccoon_cli.commands import missions as missions_module
        src = inspect.getsource(missions_module.create_command.callback)
        assert "add_mission_import_to_main" not in src

    def test_get_next_mission_number_exists_in_shared_layer(self):
        """Mission-number logic lives in the shared layer, not buried in CLI."""
        from raccoon_cli.project_creation import get_next_mission_number
        assert callable(get_next_mission_number)


# ---------------------------------------------------------------------------
# Mission length
# ---------------------------------------------------------------------------

def _write_long_mission(project: Path, filename: str, step_count: int) -> None:
    steps = "\n".join(f"            step_{i}()," for i in range(step_count))
    (project / "src" / "missions" / filename).write_text(
        "class M010LongMission(Mission):\n"
        "    def sequence(self) -> Sequential:\n"
        "        return seq([\n"
        f"{steps}\n"
        "        ])\n",
        encoding="utf-8",
    )


def _set_config(project: Path, **validation_options) -> None:
    path = project / "raccoon.project.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["validation"] = validation_options
    path.write_text(yaml.dump(config), encoding="utf-8")


class TestMissionLength:
    def _project(self, tmp_path: Path, steps: int, **config) -> Path:
        project = _make_project(
            tmp_path,
            ["M010LongMission"],
            ["m010_long_mission.py"],
            ["from .missions.m010_long_mission import M010LongMission"],
        )
        _write_long_mission(project, "m010_long_mission.py", steps)
        if config:
            _set_config(project, **config)
        return project

    def test_default_limit_is_not_exceeded_at_limit(self, tmp_path):
        project = self._project(tmp_path, validation.DEFAULT_MAX_MISSION_STEPS)
        assert "mission_too_long" not in _codes(validate_project(project, python_compile=False))

    def test_default_limit_exceeded_warns(self, tmp_path):
        project = self._project(tmp_path, validation.DEFAULT_MAX_MISSION_STEPS + 1)
        result = validate_project(project, python_compile=False)
        assert "mission_too_long" in _codes(result)
        assert not result.has_errors

    def test_custom_limit_is_respected(self, tmp_path):
        project = self._project(tmp_path, 6, max_mission_steps=5)
        assert "mission_too_long" in _codes(validate_project(project, python_compile=False))

    def test_raised_limit_silences_mission_warning(self, tmp_path):
        project = self._project(tmp_path, 30, max_mission_steps=40)
        codes = _codes(validate_project(project, python_compile=False))
        assert "mission_too_long" not in codes
        assert "max_mission_steps_too_high" not in codes

    def test_limit_above_three_times_default_warns(self, tmp_path):
        too_high = validation.DEFAULT_MAX_MISSION_STEPS * validation.MAX_MISSION_STEPS_WARN_FACTOR + 1
        project = self._project(tmp_path, 3, max_mission_steps=too_high)
        assert "max_mission_steps_too_high" in _codes(validate_project(project, python_compile=False))

    def test_limit_at_three_times_default_does_not_warn(self, tmp_path):
        limit = validation.DEFAULT_MAX_MISSION_STEPS * validation.MAX_MISSION_STEPS_WARN_FACTOR
        project = self._project(tmp_path, 3, max_mission_steps=limit)
        assert "max_mission_steps_too_high" not in _codes(validate_project(project, python_compile=False))

    def test_invalid_limit_warns_and_uses_default(self, tmp_path):
        project = self._project(tmp_path, validation.DEFAULT_MAX_MISSION_STEPS + 1, max_mission_steps="lots")
        codes = _codes(validate_project(project, python_compile=False))
        assert "invalid_max_mission_steps" in codes
        assert "mission_too_long" in codes

    def test_mission_without_seq_list_is_ignored(self, tmp_path):
        project = self._project(tmp_path, 1)
        (project / "src" / "missions" / "m010_long_mission.py").write_text(
            "class M010LongMission(Mission):\n"
            "    def sequence(self):\n"
            "        return build_everything()\n",
            encoding="utf-8",
        )
        assert "mission_too_long" not in _codes(validate_project(project, python_compile=False))


class TestRepeatedSteps:
    def _project(self, tmp_path: Path, steps: list[str]) -> Path:
        project = _make_project(
            tmp_path,
            ["M010LongMission"],
            ["m010_long_mission.py"],
            ["from .missions.m010_long_mission import M010LongMission"],
        )
        body = "\n".join(f"            {s}," for s in steps)
        (project / "src" / "missions" / "m010_long_mission.py").write_text(
            "class M010LongMission(Mission):\n"
            "    def sequence(self) -> Sequential:\n"
            "        return seq([\n"
            f"{body}\n"
            "        ])\n",
            encoding="utf-8",
        )
        return project

    def _issues(self, project: Path):
        result = validate_project(project, python_compile=False)
        return [i for i in result.issues if i.code == "repeated_steps"]

    def test_three_identical_steps_in_a_row_warn_with_loop_for_hint(self, tmp_path):
        project = self._project(tmp_path, ["a()", "drive_forward(10)", "drive_forward(10)", "drive_forward(10)", "b()"])
        issues = self._issues(project)
        assert len(issues) == 1
        assert "3 times" in issues[0].message
        assert "loop_for(<step>, iterations=3)" in issues[0].hint

    def test_two_identical_steps_do_not_warn(self, tmp_path):
        project = self._project(tmp_path, ["drive_forward(10)", "drive_forward(10)", "b()"])
        assert self._issues(project) == []

    def test_identical_steps_that_are_not_adjacent_do_not_warn(self, tmp_path):
        project = self._project(tmp_path, ["a()", "b()", "a()", "b()", "a()"][:2] + ["a()", "c()", "a()"])
        assert self._issues(project) == []

    def test_repeated_block_of_two_steps_warns(self, tmp_path):
        project = self._project(tmp_path, ["a()", "b()"] * 3 + ["c()"])
        issues = self._issues(project)
        assert len(issues) == 1
        assert "block of 2 steps" in issues[0].message
        assert "loop_for(seq([...]), iterations=3)" in issues[0].hint

    def test_different_arguments_are_not_a_repetition(self, tmp_path):
        project = self._project(tmp_path, ["drive_forward(10)", "drive_forward(20)", "drive_forward(30)"])
        assert self._issues(project) == []


class TestUncommentedSteps:
    def _project(self, tmp_path: Path, body: str, **config) -> Path:
        project = _make_project(
            tmp_path,
            ["M010LongMission"],
            ["m010_long_mission.py"],
            ["from .missions.m010_long_mission import M010LongMission"],
        )
        (project / "src" / "missions" / "m010_long_mission.py").write_text(
            "class M010LongMission(Mission):\n"
            "    def sequence(self) -> Sequential:\n"
            "        return seq([\n"
            f"{body}"
            "        ])\n",
            encoding="utf-8",
        )
        if config:
            _set_config(project, **config)
        return project

    @staticmethod
    def _steps(count: int, start: int = 0) -> str:
        return "".join(f"            step_{i}(),\n" for i in range(start, start + count))

    def _codes(self, project: Path) -> list[str]:
        return [i.code for i in validate_project(project, python_compile=False).issues]

    def test_seven_uncommented_steps_warn(self, tmp_path):
        assert "mission_uncommented" in self._codes(self._project(tmp_path, self._steps(7)))

    def test_six_uncommented_steps_do_not_warn(self, tmp_path):
        assert "mission_uncommented" not in self._codes(self._project(tmp_path, self._steps(6)))

    def test_comment_above_breaks_the_run(self, tmp_path):
        body = self._steps(5) + "            # now the second part\n" + self._steps(6, 5)
        assert "mission_uncommented" not in self._codes(self._project(tmp_path, body))

    def test_comment_covers_only_one_step(self, tmp_path):
        body = "            # first part\n" + self._steps(8)
        assert "mission_uncommented" in self._codes(self._project(tmp_path, body))

    def test_trailing_comment_on_step_line_counts(self, tmp_path):
        body = self._steps(5) + "            step_x(),  # explained\n" + self._steps(6, 5)
        assert "mission_uncommented" not in self._codes(self._project(tmp_path, body))

    def test_hash_inside_a_string_is_not_a_comment(self, tmp_path):
        body = self._steps(3) + '            say("# not a comment"),\n' + self._steps(4, 3)
        assert "mission_uncommented" in self._codes(self._project(tmp_path, body))

    def test_message_names_file_lines_and_count(self, tmp_path):
        project = self._project(tmp_path, self._steps(8))
        issue = next(i for i in validate_project(project, python_compile=False).issues if i.code == "mission_uncommented")
        assert "m010_long_mission.py:4-11" in issue.message
        assert "8 steps" in issue.message

    def test_option_false_disables_the_check(self, tmp_path):
        project = self._project(tmp_path, self._steps(10), check_mission_comments=False)
        assert "mission_uncommented" not in self._codes(project)

    def test_invalid_option_warns_and_keeps_check_on(self, tmp_path):
        project = self._project(tmp_path, self._steps(10), check_mission_comments="nope")
        codes = self._codes(project)
        assert "invalid_check_mission_comments" in codes
        assert "mission_uncommented" in codes


class TestValidationSection:
    def test_options_outside_validation_key_are_ignored(self, tmp_path):
        project = _make_project(
            tmp_path,
            ["M010LongMission"],
            ["m010_long_mission.py"],
            ["from .missions.m010_long_mission import M010LongMission"],
        )
        _write_long_mission(project, "m010_long_mission.py", validation.DEFAULT_MAX_MISSION_STEPS + 1)
        path = project / "raccoon.project.yml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        config["max_mission_steps"] = 100
        path.write_text(yaml.dump(config), encoding="utf-8")
        assert "mission_too_long" in _codes(validate_project(project, python_compile=False))

    def test_non_mapping_validation_section_warns(self, tmp_path):
        project = _make_project(tmp_path, [])
        path = project / "raccoon.project.yml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        config["validation"] = 5
        path.write_text(yaml.dump(config), encoding="utf-8")
        assert "invalid_validation_section" in _codes(validate_project(project, python_compile=False))


class TestSingleElementComposites:
    def _codes_for(self, tmp_path: Path, expression: str) -> list[str]:
        _write_config(tmp_path)
        _write_raw_mission(tmp_path, "m010_go_mission.py", f"step = {expression}\n")
        result = validate_project(tmp_path, codegen_check=False, defs_check=False)
        return [i.code for i in result.warnings if i.code.startswith("single_element_")]

    @pytest.mark.parametrize("expression", [
        "seq([a()])",
        "outer(seq([a()]))",
    ])
    def test_seq_with_one_step_warns(self, tmp_path: Path, expression: str):
        assert self._codes_for(tmp_path, expression) == ["single_element_seq"]

    @pytest.mark.parametrize("expression", [
        "parallel(a())",
        "parallel([a(), b()])",
        "parallel(seq([a(), b()]))",
    ])
    def test_parallel_with_one_argument_warns(self, tmp_path: Path, expression: str):
        assert self._codes_for(tmp_path, expression) == ["single_element_parallel"]

    @pytest.mark.parametrize("expression", [
        "seq([])",
        "seq([a(), b()])",
        "seq([*steps])",
        "seq(steps)",
        "parallel(a(), b())",
        "parallel(*branches)",
        "other.parallel(a())",
    ])
    def test_no_warning(self, tmp_path: Path, expression: str):
        assert self._codes_for(tmp_path, expression) == []

    @pytest.mark.parametrize(("returned", "expected"), [
        ("parallel(a())", []),
        ("parallel([a(), b()])", []),
        ("parallel(parallel(a()))", ["single_element_parallel"]),
        ("seq([a()])", []),
        ("seq([seq([a()])])", ["single_element_seq"]),
        ("seq([parallel(a())])", ["single_element_parallel"]),
    ])
    def test_step_returned_by_a_mission_sequence(
        self, tmp_path: Path, returned: str, expected: list[str],
    ):
        _write_config(tmp_path)
        _write_raw_mission(
            tmp_path, "m010_go_mission.py",
            f"class M010GoMission:\n    def sequence(self):\n        return {returned}\n",
        )
        result = validate_project(tmp_path, codegen_check=False, defs_check=False)
        codes = [i.code for i in result.warnings if i.code.startswith("single_element_")]
        assert codes == expected
