"""Tests for raccoon_cli/validate.py — project consistency validation.

All tests use real temporary directories (no mocks) so that file-system
interactions are caught exactly as they would be at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
        """raccoon create mission must not insert imports into main.py."""
        import inspect
        from raccoon_cli.commands import create as create_module
        src = inspect.getsource(create_module.create_mission_command.callback)
        assert "add_mission_import_to_main" not in src

    def test_get_next_mission_number_exists_in_shared_layer(self):
        """Mission-number logic lives in the shared layer, not buried in CLI."""
        from raccoon_cli.project_creation import get_next_mission_number
        assert callable(get_next_mission_number)
