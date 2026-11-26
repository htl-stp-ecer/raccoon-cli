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


class TestExplodeOperators:
    CONFIG = FormattingConfig(explode_operators=("until",))

    def test_every_condition_gets_its_own_line(self):
        out = format_source("x = drive().until(a() + b() + c())\n", self.CONFIG)
        assert out == "x = drive().until(\n    a()\n    + b()\n    + c()\n)\n"

    def test_only_the_outermost_precedence_is_split(self):
        out = format_source("x = drive().until(a + b | c)\n", self.CONFIG)
        assert out == "x = drive().until(\n    a + b\n    | c\n)\n"

    def test_result_is_stable_and_has_no_markers(self):
        out = format_source("x = drive().until(a > (b) > c)\n", self.CONFIG)
        assert "__raccoon" not in out
        assert format_source(out, self.CONFIG) == out

    def test_single_condition_gets_its_own_line(self):
        out = format_source("x = drive().until(a())\n", self.CONFIG)
        assert out == "x = drive().until(\n    a()\n)\n"
        assert format_source(out, self.CONFIG) == out

    def test_long_comment_does_not_break_the_condition(self):
        comment = "# " + "long " * 20 + "comment"
        source = f"x = drive().until(\n    over_line(\n        rear.left\n    )  {comment}\n    + after_cm(6)\n)\n"
        out = format_source(source, self.CONFIG)
        assert out == f"x = drive().until(\n    over_line(rear.left)  {comment}\n    + after_cm(6)\n)\n"
        assert format_source(out, self.CONFIG) == out

    def test_too_long_condition_is_still_broken(self):
        source = f"x = drive().until(a() + over_line({'b' * 50}, {'c' * 50}))  # end\n"
        out = format_source(source, self.CONFIG)
        assert f"        {'b' * 50},\n" in out
        assert out.count("# end") == 1
        assert format_source(out, self.CONFIG) == out

    def test_off_by_default(self):
        source = "x = drive().until(a + b)\n"
        assert format_source(source, FormattingConfig()) == source


class TestCommentsAndLineLength:
    LONG = "# " + "long " * 20 + "comment"

    def test_long_comment_does_not_break_the_code(self):
        source = f"x = foo(a, b)  {self.LONG}\n"
        assert format_source(source, DEFAULTS) == source

    def test_broken_line_is_joined_again(self):
        out = format_source(f"x = foo(\n    a, b\n)  {self.LONG}\n", DEFAULTS)
        assert out == f"x = foo(a, b)  {self.LONG}\n"

    def test_comment_stays_behind_its_code_when_a_line_is_too_long(self):
        source = f"x = foo(\n    a,  {self.LONG}\n    b\n)\n"
        out = format_source(source, DEFAULTS)
        assert f"a,  {self.LONG}\n" in out
        assert format_source(out, DEFAULTS) == out

    def test_directives_are_left_to_black(self):
        out = format_source(f"x = foo(a, b)  # noqa {'x' * 90}\n", DEFAULTS)
        assert out.startswith("x = foo(\n    a, b\n)")


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


class TestExplodeCalls:
    def test_short_parallel_is_one_argument_per_line(self):
        out = format_source("x = parallel(stop_camera(), motor_off(Defs.m))\n", DEFAULTS)
        assert out == "x = parallel(\n    stop_camera(),\n    motor_off(Defs.m),\n)\n"

    def test_reported_shutdown_mission(self):
        source = (
            "class M999ShutdownMission(Mission):\n"
            "    def sequence(self) -> Sequential:\n"
            "        return seq([\n"
            "            parallel(\n"
            "                stop_camera(), fully_disable_servos(), motor_off(Defs.cone_pusher_motor)\n"
            "            ),\n"
            "        ])\n"
        )
        assert format_source(source, DEFAULTS).splitlines()[3:8] == [
            "            parallel(",
            "                stop_camera(),",
            "                fully_disable_servos(),",
            "                motor_off(Defs.cone_pusher_motor),",
            "            ),",
        ]

    def test_short_seq_list_is_exploded_and_stays_hugged(self):
        assert format_source("x = seq([a(), b()])\n", DEFAULTS) == "x = seq([\n    a(),\n    b(),\n])\n"

    def test_single_argument_is_exploded_too(self):
        assert format_source("x = parallel(a())\n", DEFAULTS) == "x = parallel(\n    a(),\n)\n"

    def test_existing_trailing_comma_is_not_doubled(self):
        out = format_source("x = parallel(a(), b(),)\n", DEFAULTS)
        assert ",," not in out

    def test_parenthesised_last_argument_stays_an_expression(self):
        out = format_source("x = parallel(a, (b + c))\n", DEFAULTS)
        assert out == "x = parallel(\n    a,\n    (b + c),\n)\n"

    def test_comment_after_last_argument_is_kept(self):
        out = format_source("x = parallel(\n    a(),\n    b()  # last\n)\n", DEFAULTS)
        assert "b(),  # last" in out

    def test_other_calls_are_left_to_black(self):
        assert format_source("x = foo(a(), b())\n", DEFAULTS) == "x = foo(a(), b())\n"

    def test_call_without_arguments_is_untouched(self):
        assert format_source("x = parallel()\n", DEFAULTS) == "x = parallel()\n"

    def test_empty_list_argument_is_untouched(self):
        assert format_source("x = seq([])\n", DEFAULTS) == "x = seq([])\n"

    def test_empty_list_inside_other_arguments_is_untouched(self):
        out = format_source("x = parallel(a(), [])\n", DEFAULTS)
        assert out == "x = parallel(\n    a(),\n    [],\n)\n"

    def test_generator_argument_is_not_broken(self):
        assert format_source("x = seq(a for a in steps)\n", DEFAULTS) == "x = seq(a for a in steps)\n"

    def test_method_calls_match_by_attribute_name(self):
        out = format_source("x = steps.parallel(a(), b())\n", DEFAULTS)
        assert out == "x = steps.parallel(\n    a(),\n    b(),\n)\n"

    def test_names_are_configurable(self):
        config = FormattingConfig(explode_calls=("background",))
        assert format_source("x = background(a(), b())\n", config).count("\n") == 4
        assert format_source("x = parallel(a(), b())\n", config) == "x = parallel(a(), b())\n"

    def test_can_be_turned_off_with_an_empty_list(self):
        config = FormattingConfig(explode_calls=())
        assert format_source("x = parallel(a(), b())\n", config) == "x = parallel(a(), b())\n"

    def test_result_is_stable_and_keeps_meaning(self):
        source = "x = seq([a(), parallel(b(), c())])\n"
        once = format_source(source, DEFAULTS)
        assert format_source(once, DEFAULTS) == once
        assert ast.dump(ast.parse(source)) == ast.dump(ast.parse(once))

    def test_non_ascii_text_before_the_call_does_not_shift_the_comma(self):
        out = format_source('x = parallel(say("äöü"), b())\n', DEFAULTS)
        assert 'say("äöü"),' in out


class TestConfig:
    def test_defaults_without_file(self, tmp_path):
        assert load_formatting_config(tmp_path) == FormattingConfig()

    def test_empty_file_gives_defaults(self, tmp_path):
        (tmp_path / "raccoon.formatting.yml").write_text("", encoding="utf-8")
        assert load_formatting_config(tmp_path) == FormattingConfig()

    def test_values_are_read(self, tmp_path):
        (tmp_path / "raccoon.formatting.yml").write_text(
            yaml.dump({"auto_format": True, "line_length": 100, "exclude": ["src/legacy/**"]}),
            encoding="utf-8",
        )
        config = load_formatting_config(tmp_path)
        assert config.auto_format is True
        assert config.line_length == 100
        assert config.exclude == ("src/legacy/**",)

    def test_unknown_key_is_rejected(self):
        with pytest.raises(FormattingConfigError, match="line_lenght"):
            parse_formatting_config({"line_lenght": 100})

    @pytest.mark.parametrize(
        "raw",
        [
            {"auto_format": "yes"},
            {"line_length": "88"},
            {"line_length": True},
            {"line_length": 5},
            {"exclude": "src/hardware/**"},
            {"exclude": [1]},
            {"explode_calls": "seq"},
            {"explode_calls": [1]},
            {"explode_operators": "until"},
            ["auto_format"],
        ],
    )
    def test_bad_values_are_rejected(self, raw):
        with pytest.raises(FormattingConfigError):
            parse_formatting_config(raw)

    def test_enabled_is_not_an_option(self):
        with pytest.raises(FormattingConfigError, match="enabled"):
            parse_formatting_config({"enabled": False})

    def test_invalid_yaml_is_rejected(self, tmp_path):
        (tmp_path / "raccoon.formatting.yml").write_text("auto_format: [", encoding="utf-8")
        with pytest.raises(FormattingConfigError):
            load_formatting_config(tmp_path)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "missions").mkdir(parents=True)
    (tmp_path / "src" / "hardware").mkdir()
    (tmp_path / "raccoon.project.yml").write_text(
        yaml.dump({"name": "T", "uuid": "u", "format_version": 2, "missions": []}),
        encoding="utf-8",
    )
    (tmp_path / "raccoon.formatting.yml").write_text("", encoding="utf-8")
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


class TestMissingConfigFile:
    def _without_config(self, tmp_path: Path) -> Path:
        project = _project(tmp_path)
        (project / "raccoon.formatting.yml").unlink()
        return project

    def test_project_function_formats_nothing(self, tmp_path):
        project = self._without_config(tmp_path)
        result = format_project(project)
        assert result.config_missing
        assert result.changed == []
        assert (project / "src" / "missions" / "m010_a_mission.py").read_text() == MISSION

    def test_command_warns_and_changes_nothing(self, tmp_path, monkeypatch):
        project = self._without_config(tmp_path)
        monkeypatch.chdir(project)
        result = CliRunner().invoke(main, ["--no-validate", "format"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "raccoon.formatting.yml" in result.output
        assert "nothing was formatted" in result.output
        assert (project / "src" / "missions" / "m010_a_mission.py").read_text() == MISSION

    def test_check_also_warns_and_changes_nothing(self, tmp_path, monkeypatch):
        project = self._without_config(tmp_path)
        monkeypatch.chdir(project)
        result = CliRunner().invoke(main, ["--no-validate", "format", "--check"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "nothing was formatted" in result.output

    def test_empty_file_is_enough_and_uses_defaults(self, tmp_path):
        project = self._without_config(tmp_path)
        (project / "raccoon.formatting.yml").write_text("", encoding="utf-8")
        result = format_project(project)
        assert not result.config_missing
        assert len(result.changed) == 1


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

    def test_run_formats_when_auto_format_is_true(self, tmp_path, monkeypatch):
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
