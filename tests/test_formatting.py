"""Tests for raccoon_cli/formatting.py and the ``raccoon format`` command."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from raccoon_cli import cli
from raccoon_cli.cli import main
from raccoon_cli.formatting import (
    FormattingConfig,
    FormattingConfigError,
    format_project,
    format_source,
    load_formatting_config,
    parse_formatting_config,
)

MISSION = '''\
class M010Mission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            # first part
            drive_forward().until(
                over_line(Defs.front.left),
            ),
            wait_for_background(
                name="prep_arm"
            ),


            turn_left(90),

            # second part
            background(
                step=drop(),
                name="drop"
            ),
        ])
'''

DEFAULTS = FormattingConfig()


class TestFormatSource:
    def test_seq_bracket_stays_hugged(self):
        out = format_source(MISSION, DEFAULTS)
        assert "return seq([" in out
        assert "        ])" in out

    def test_steps_keep_their_indentation_level(self):
        out = format_source(MISSION, DEFAULTS)
        assert "            turn_left(90)," in out

    def test_hug_can_be_turned_off(self):
        out = format_source(MISSION, FormattingConfig(hug_brackets=False))
        assert "return seq(\n" in out

    def test_blank_lines_in_brackets_are_kept(self):
        out = format_source(MISSION, DEFAULTS)
        assert 'wait_for_background(name="prep_arm"),\n\n            turn_left(90),' in out

    def test_several_blank_lines_collapse_to_one(self):
        out = format_source(MISSION, DEFAULTS)
        assert "\n\n\n            turn_left" not in out

    def test_blank_lines_in_brackets_can_be_dropped(self):
        out = format_source(MISSION, FormattingConfig(keep_blank_lines=False))
        assert 'wait_for_background(name="prep_arm"),\n            turn_left(90),' in out

    def test_no_marker_is_left_behind(self):
        assert "__raccoon_blank__" not in format_source(MISSION, DEFAULTS)

    def test_blank_line_right_after_opening_bracket_is_dropped(self):
        out = format_source("x = foo(\n\n    1,\n    2,\n)\n", DEFAULTS)
        assert out == "x = foo(\n    1,\n    2,\n)\n"

    def test_blank_line_right_before_closing_bracket_is_dropped(self):
        out = format_source("x = foo(\n    1,\n    2,\n\n)\n", DEFAULTS)
        assert out == "x = foo(\n    1,\n    2,\n)\n"

    def test_blank_lines_inside_multiline_string_are_untouched(self):
        source = 'x = foo("""a\n\n\nb""", 1)\n'
        assert '"""a\n\n\nb"""' in format_source(source, DEFAULTS)

    def test_top_level_blank_lines_follow_black(self):
        out = format_source("def a():\n    pass\ndef b():\n    pass\n", DEFAULTS)
        assert out == "def a():\n    pass\n\n\ndef b():\n    pass\n"

    def test_result_is_stable(self):
        once = format_source(MISSION, DEFAULTS)
        assert format_source(once, DEFAULTS) == once

    def test_meaning_of_code_is_unchanged(self):
        assert ast.dump(ast.parse(MISSION)) == ast.dump(ast.parse(format_source(MISSION, DEFAULTS)))

    def test_line_length_is_respected(self):
        source = "x = some_function(argument_one, argument_two, argument_three)\n"
        assert format_source(source, FormattingConfig(line_length=40)).count("\n") > 1
        assert format_source(source, FormattingConfig(line_length=100)).count("\n") == 1

    def test_string_normalization_can_be_skipped(self):
        assert format_source("x = 'a'\n", DEFAULTS) == 'x = "a"\n'
        assert format_source("x = 'a'\n", FormattingConfig(skip_string_normalization=True)) == "x = 'a'\n"

    def test_invalid_code_raises(self):
        with pytest.raises(Exception):
            format_source("def broken(:\n", DEFAULTS)


class TestConfig:
    def test_defaults_without_file(self, tmp_path):
        assert load_formatting_config(tmp_path) == FormattingConfig()

    def test_empty_file_gives_defaults(self, tmp_path):
        (tmp_path / "raccoon.formatting.yml").write_text("", encoding="utf-8")
        assert load_formatting_config(tmp_path) == FormattingConfig()

    def test_values_are_read(self, tmp_path):
        (tmp_path / "raccoon.formatting.yml").write_text(
            yaml.dump({"enabled": False, "line_length": 100, "exclude": ["src/legacy/**"]}),
            encoding="utf-8",
        )
        config = load_formatting_config(tmp_path)
        assert config.enabled is False
        assert config.line_length == 100
        assert config.exclude == ("src/legacy/**",)

    def test_unknown_key_is_rejected(self):
        with pytest.raises(FormattingConfigError, match="line_lenght"):
            parse_formatting_config({"line_lenght": 100})

    @pytest.mark.parametrize(
        "raw",
        [
            {"enabled": "yes"},
            {"line_length": "88"},
            {"line_length": True},
            {"line_length": 5},
            {"exclude": "src/hardware/**"},
            {"exclude": [1]},
            ["enabled"],
        ],
    )
    def test_bad_values_are_rejected(self, raw):
        with pytest.raises(FormattingConfigError):
            parse_formatting_config(raw)

    def test_invalid_yaml_is_rejected(self, tmp_path):
        (tmp_path / "raccoon.formatting.yml").write_text("enabled: [", encoding="utf-8")
        with pytest.raises(FormattingConfigError):
            load_formatting_config(tmp_path)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "missions").mkdir(parents=True)
    (tmp_path / "src" / "hardware").mkdir()
    (tmp_path / "raccoon.project.yml").write_text(
        yaml.dump({"name": "T", "uuid": "u", "format_version": 2, "missions": []}),
        encoding="utf-8",
    )
    (tmp_path / "src" / "missions" / "m010_a_mission.py").write_text(MISSION, encoding="utf-8")
    (tmp_path / "src" / "hardware" / "defs.py").write_text("x = 'generated'\n", encoding="utf-8")
    return tmp_path


class TestFormatProject:
    def test_formats_files_and_skips_hardware(self, tmp_path):
        project = _project(tmp_path)
        result = format_project(project)
        assert [p.as_posix() for p in result.changed] == ["src/missions/m010_a_mission.py"]
        assert (project / "src" / "hardware" / "defs.py").read_text() == "x = 'generated'\n"
        assert "return seq([" in (project / "src" / "missions" / "m010_a_mission.py").read_text()

    def test_second_run_changes_nothing(self, tmp_path):
        project = _project(tmp_path)
        format_project(project)
        assert format_project(project).changed == []

    def test_check_writes_nothing(self, tmp_path):
        project = _project(tmp_path)
        result = format_project(project, check=True)
        assert len(result.changed) == 1
        assert (project / "src" / "missions" / "m010_a_mission.py").read_text() == MISSION

    def test_disabled_does_nothing(self, tmp_path):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("enabled: false\n", encoding="utf-8")
        result = format_project(project)
        assert result.disabled
        assert result.changed == []
        assert (project / "src" / "missions" / "m010_a_mission.py").read_text() == MISSION

    def test_custom_exclude_replaces_default(self, tmp_path):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text(
            yaml.dump({"exclude": ["src/missions/**"]}), encoding="utf-8"
        )
        result = format_project(project)
        assert [p.as_posix() for p in result.changed] == ["src/hardware/defs.py"]

    def test_file_with_syntax_error_is_reported_and_others_continue(self, tmp_path):
        project = _project(tmp_path)
        (project / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        result = format_project(project)
        assert [p.as_posix() for p, _ in result.errors] == ["src/broken.py"]
        assert len(result.changed) == 1

    def test_crlf_line_endings_are_preserved(self, tmp_path):
        project = _project(tmp_path)
        path = project / "src" / "crlf.py"
        path.write_bytes(b"x = 'a'\r\ny = 1\r\n")
        format_project(project)
        assert path.read_bytes() == b'x = "a"\r\ny = 1\r\n'


class TestFormatCommand:
    def _run(self, project: Path, *args: str, monkeypatch):
        monkeypatch.chdir(project)
        return CliRunner().invoke(main, ["--no-validate", "format", *args], catch_exceptions=False)

    def test_formats_project(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        result = self._run(project, monkeypatch=monkeypatch)
        assert result.exit_code == 0
        assert "m010_a_mission.py" in result.output
        assert "return seq([" in (project / "src" / "missions" / "m010_a_mission.py").read_text()

    def test_check_exits_one_when_files_would_change(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        result = self._run(project, "--check", monkeypatch=monkeypatch)
        assert result.exit_code == 1
        assert "would reformat" in result.output
        assert (project / "src" / "missions" / "m010_a_mission.py").read_text() == MISSION

    def test_check_exits_zero_when_formatted(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        format_project(project)
        assert self._run(project, "--check", monkeypatch=monkeypatch).exit_code == 0

    def test_disabled_project_prints_notice_and_exits_zero(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("enabled: false\n", encoding="utf-8")
        result = self._run(project, monkeypatch=monkeypatch)
        assert result.exit_code == 0
        assert "disabled" in result.output
        assert (project / "src" / "missions" / "m010_a_mission.py").read_text() == MISSION

    def test_bad_config_exits_one(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("line_lenght: 5\n", encoding="utf-8")
        result = self._run(project, monkeypatch=monkeypatch)
        assert result.exit_code == 1
        assert "line_lenght" in result.output


class _FakeConsole:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, text="", *args, **kwargs):
        self.lines.append(str(text))


class _FakeContext:
    def __init__(self, subcommand: str):
        self.invoked_subcommand = subcommand
        self.obj = {"console": _FakeConsole()}


class TestAutoFormat:
    def _mission(self, project: Path) -> str:
        return (project / "src" / "missions" / "m010_a_mission.py").read_text()

    def _auto(self, project: Path, monkeypatch, subcommand: str = "run") -> _FakeContext:
        monkeypatch.chdir(project)
        ctx = _FakeContext(subcommand)
        cli._run_auto_format(ctx)
        return ctx

    def test_default_is_off(self):
        assert FormattingConfig().auto_format is False

    def test_run_does_not_format_by_default(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        self._auto(project, monkeypatch)
        assert self._mission(project) == MISSION

    def test_run_formats_when_enabled(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("auto_format: true\n", encoding="utf-8")
        ctx = self._auto(project, monkeypatch)
        assert "return seq([" in self._mission(project)
        assert any("m010_a_mission.py" in line for line in ctx.obj["console"].lines)

    def test_other_commands_do_not_format(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("auto_format: true\n", encoding="utf-8")
        self._auto(project, monkeypatch, subcommand="sync")
        assert self._mission(project) == MISSION

    def test_enabled_false_wins_over_auto_format(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("enabled: false\nauto_format: true\n", encoding="utf-8")
        self._auto(project, monkeypatch)
        assert self._mission(project) == MISSION

    def test_broken_file_is_skipped_with_warning_and_run_goes_on(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("auto_format: true\n", encoding="utf-8")
        (project / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        ctx = self._auto(project, monkeypatch)
        assert any("skipped src/broken.py" in line for line in ctx.obj["console"].lines)
        assert "return seq([" in self._mission(project)

    def test_bad_config_warns_and_does_not_abort(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").write_text("auto_formt: true\n", encoding="utf-8")
        ctx = self._auto(project, monkeypatch)
        assert any("skipping auto-format" in line for line in ctx.obj["console"].lines)
        assert self._mission(project) == MISSION

    def test_outside_a_project_does_nothing(self, tmp_path, monkeypatch):
        ctx = self._auto(tmp_path, monkeypatch)
        assert ctx.obj["console"].lines == []
