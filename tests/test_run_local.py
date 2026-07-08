"""Tests for local run command — Ctrl+C handling via Popen."""

import subprocess
import signal
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from raccoon_cli.commands.run import (
    _ensure_single_active_program,
    _run_local,
    _run_local_streaming_jsonl,
)


class FakePopen:
    """Minimal Popen stub whose wait() behaviour is configurable."""

    def __init__(self, *, returncode=0, wait_side_effect=None, output_lines=()):
        self.pid = 4242
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


@pytest.fixture(autouse=True)
def patch_single_program_guard():
    """Prevent tests from touching real process state or signal handlers."""
    with patch("raccoon_cli.commands.run._active_program_lock", return_value=nullcontext()), \
         patch("raccoon_cli.commands.run._ensure_single_active_program"), \
         patch("raccoon_cli.commands.run._write_active_program_state"), \
         patch("raccoon_cli.commands.run._clear_active_program_state"), \
         patch("raccoon_cli.commands.run._install_termination_handlers", return_value=(None, None)), \
         patch("raccoon_cli.commands.run._restore_termination_handlers"), \
         patch("raccoon_cli.commands.run._sensor_rings_present", return_value=False), \
         patch("raccoon_cli.commands.run.sys.stdout.isatty", return_value=True):
        yield


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
         patch("raccoon_cli.commands.run._kill_process_group") as mock_kill, \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):

        with pytest.raises(SystemExit):
            _run_local(
                click_ctx, fake_project, {"name": "test"}, args=(),
                no_codegen=True,
            )

    assert mock_kill.call_args_list == [call(fake.pid, signal.SIGTERM)]


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
         patch("raccoon_cli.commands.run._kill_process_group") as mock_kill, \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):

        with pytest.raises(SystemExit):
            _run_local(
                click_ctx, fake_project, {"name": "test"}, args=(),
                no_codegen=True,
            )

    assert mock_kill.call_args_list == [
        call(fake.pid, signal.SIGTERM),
        call(fake.pid, signal.SIGKILL),
    ]


def test_launches_src_main_in_own_session(fake_project, click_ctx):
    """Robot program is launched in its own POSIX session for group cleanup."""
    fake = FakePopen(returncode=0)

    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake) as mock_popen, \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):
        _run_local(
            click_ctx, fake_project, {"name": "test"}, args=(),
            no_codegen=True,
        )

    assert mock_popen.call_args.kwargs["start_new_session"] is True


class _StreamPopen:
    """Popen stub that reports 'finished' via poll() for the JSONL streamer."""

    def __init__(self, returncode=0):
        self.pid = 4243
        self.returncode = returncode

    def poll(self):
        # Already exited → wait_for_path returns at once and follow_lines drains.
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def test_streaming_jsonl_echoes_log_lines_to_stdout(fake_project, capsys):
    """The Pi-side streamer re-emits each tailed JSONL line to stdout verbatim."""
    console = MagicMock()
    run_dir = fake_project / ".raccoon" / "runs" / "20260707T120000Z"
    run_dir.mkdir(parents=True)
    log_path = run_dir / "libstp.jsonl"
    lines = [
        '{"t":"2026-07-07T12:00:00","elapsed":0.01,"level":"info","msg":"a"}',
        '{"t":"2026-07-07T12:00:01","elapsed":0.02,"level":"warning","msg":"b"}',
    ]
    log_path.write_text("\n".join(lines) + "\n")

    fake = _StreamPopen(returncode=0)
    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake):
        rc = _run_local_streaming_jsonl(
            ["true"], fake_project, {}, console, log_path=log_path
        )

    assert rc == 0
    out = capsys.readouterr().out
    # Both raw JSON lines reached stdout (→ relayed over the WS to the laptop).
    assert lines[0] in out
    assert lines[1] in out


def test_streaming_jsonl_drops_trace_over_the_wire(fake_project, capsys):
    """TRACE lines are filtered out before hitting stdout; DEBUG+ still stream."""
    console = MagicMock()
    run_dir = fake_project / ".raccoon" / "runs" / "20260707T120001Z"
    run_dir.mkdir(parents=True)
    log_path = run_dir / "libstp.jsonl"
    trace = '{"t":"2026-07-07T12:00:00","elapsed":0.01,"level":"trace","msg":"noise"}'
    debug = '{"t":"2026-07-07T12:00:01","elapsed":0.02,"level":"debug","msg":"Preloading main mission: MFoo"}'
    info = '{"t":"2026-07-07T12:00:02","elapsed":0.03,"level":"info","msg":"keep"}'
    log_path.write_text("\n".join([trace, debug, info]) + "\n")

    fake = _StreamPopen(returncode=0)
    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake):
        _run_local_streaming_jsonl(
            ["true"], fake_project, {}, console, log_path=log_path
        )

    out = capsys.readouterr().out
    assert "noise" not in out  # trace never crosses the network
    assert "Preloading main mission" in out  # debug kept (feeds the breadcrumb)
    assert "keep" in out


def test_single_program_guard_kills_stale_robot_programs(click_ctx, fake_project):
    """Before launching, stale `src.main` processes are terminated."""
    console = click_ctx.obj["console"]

    with patch("raccoon_cli.commands.run._read_active_program_state", return_value={"pid": 111}), \
         patch("raccoon_cli.commands.run._is_process_alive", return_value=True), \
         patch("raccoon_cli.commands.run._list_robot_program_pids", return_value=[111, 222]), \
         patch("raccoon_cli.commands.run._terminate_process_by_pid") as mock_kill:
        _ensure_single_active_program(fake_project, console)

    assert mock_kill.call_args_list == [call(111), call(222)]
