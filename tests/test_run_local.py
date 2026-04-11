"""Tests for local run command — Ctrl+C handling via Popen."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from raccoon_cli.commands.run import _run_local


class FakePopen:
    """Minimal Popen stub whose wait() behaviour is configurable."""

    def __init__(self, *, returncode=0, wait_side_effect=None, output_lines=()):
        self.returncode = returncode
        self._wait_side_effect = list(wait_side_effect or [])
        self.terminate_called = False
        self.kill_called = False
        self.stdout = iter(output_lines)

    def wait(self, timeout=None):
        if self._wait_side_effect:
            effect = self._wait_side_effect.pop(0)
            if isinstance(effect, type) and issubclass(effect, BaseException):
                raise effect()
            if isinstance(effect, BaseException):
                raise effect
        return self.returncode

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True
        self._wait_side_effect = []


@pytest.fixture()
def fake_project(tmp_path):
    """Minimal project directory."""
    yml = tmp_path / "raccoon.project.yml"
    yml.write_text("name: test\nuuid: test-uuid\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")
    return tmp_path


@pytest.fixture()
def click_ctx():
    """Fake Click context with console."""
    ctx = MagicMock()
    ctx.obj = {"console": MagicMock()}
    return ctx


def test_normal_exit(fake_project, click_ctx):
    """Normal execution returns exit code 0 via Popen.wait()."""
    fake = FakePopen(returncode=0)

    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake), \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline") as mock_pipe:
        mock_pipe.return_value.run_all = MagicMock()

        _run_local(
            click_ctx, fake_project, {"name": "test"}, args=(),
            no_codegen=True,
        )
    # No SystemExit means success


def test_nonzero_exit_raises(fake_project, click_ctx):
    """Non-zero exit code raises SystemExit."""
    fake = FakePopen(returncode=1)

    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake), \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):

        with pytest.raises(SystemExit) as exc_info:
            _run_local(
                click_ctx, fake_project, {"name": "test"}, args=(),
                no_codegen=True,
            )
        assert exc_info.value.code == 1


def test_keyboard_interrupt_terminates(fake_project, click_ctx):
    """Ctrl+C triggers terminate(), then clean exit."""
    fake = FakePopen(
        returncode=-2,
        wait_side_effect=[KeyboardInterrupt],
    )

    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake), \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):

        with pytest.raises(SystemExit):
            _run_local(
                click_ctx, fake_project, {"name": "test"}, args=(),
                no_codegen=True,
            )

    assert fake.terminate_called
    assert not fake.kill_called


def test_keyboard_interrupt_escalates_to_kill(fake_project, click_ctx):
    """If terminate() + wait(timeout=3) times out, kill() is called."""
    fake = FakePopen(
        returncode=-9,
        wait_side_effect=[
            KeyboardInterrupt,
            subprocess.TimeoutExpired("cmd", 3),
        ],
    )

    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake), \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):

        with pytest.raises(SystemExit):
            _run_local(
                click_ctx, fake_project, {"name": "test"}, args=(),
                no_codegen=True,
            )

    assert fake.terminate_called
    assert fake.kill_called
