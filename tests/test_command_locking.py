"""Tests for server-side command locking — one program per robot."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from raccoon_cli.server.routes.commands import (
    _active_commands,
    _cancel_running_commands_for_project,
    _reject_if_another_project_running,
)
from raccoon_cli.server.services.executor import CommandExecutor, CommandStatus


@pytest.fixture(autouse=True)
def _clean_active_commands():
    """Ensure _active_commands is empty before/after each test."""
    _active_commands.clear()
    yield
    _active_commands.clear()


def _make_executor(status: CommandStatus) -> CommandExecutor:
    """Create an executor stub with the given status and a mock cancel()."""
    executor = CommandExecutor()
    executor.status = status
    executor.cancel = AsyncMock()
    return executor


# -- _cancel_running_commands_for_project ------------------------------------

@pytest.mark.asyncio
async def test_cancel_running_for_same_project():
    """Running commands for the same project are cancelled."""
    ex = _make_executor(CommandStatus.RUNNING)
    _active_commands["cmd-1"] = {
        "project_id": "proj-A",
        "executor": ex,
    }

    await _cancel_running_commands_for_project("proj-A")

    ex.cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_does_not_touch_other_projects():
    """Commands for other projects are left alone."""
    ex = _make_executor(CommandStatus.RUNNING)
    _active_commands["cmd-1"] = {
        "project_id": "proj-B",
        "executor": ex,
    }

    await _cancel_running_commands_for_project("proj-A")

    ex.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_skips_already_finished():
    """Completed/failed commands are not cancelled again."""
    for status in (CommandStatus.COMPLETED, CommandStatus.FAILED, CommandStatus.CANCELLED):
        ex = _make_executor(status)
        _active_commands[f"cmd-{status.value}"] = {
            "project_id": "proj-A",
            "executor": ex,
        }

    await _cancel_running_commands_for_project("proj-A")

    for cmd in _active_commands.values():
        cmd["executor"].cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_pending_command():
    """PENDING commands for the same project are also cancelled."""
    ex = _make_executor(CommandStatus.PENDING)
    _active_commands["cmd-1"] = {
        "project_id": "proj-A",
        "executor": ex,
    }

    await _cancel_running_commands_for_project("proj-A")

    ex.cancel.assert_awaited_once()


# -- _reject_if_another_project_running --------------------------------------

def test_reject_when_another_project_is_running():
    """HTTP 409 when a different project has a running command."""
    from fastapi import HTTPException

    ex = _make_executor(CommandStatus.RUNNING)
    _active_commands["cmd-1"] = {
        "project_id": "proj-B",
        "executor": ex,
    }

    with pytest.raises(HTTPException) as exc_info:
        _reject_if_another_project_running("proj-A")

    assert exc_info.value.status_code == 409
    assert "proj-B" in exc_info.value.detail


def test_no_rejection_when_same_project():
    """Same-project commands don't trigger rejection (they get cancelled instead)."""
    ex = _make_executor(CommandStatus.RUNNING)
    _active_commands["cmd-1"] = {
        "project_id": "proj-A",
        "executor": ex,
    }

    # Should not raise
    _reject_if_another_project_running("proj-A")


def test_no_rejection_when_other_project_finished():
    """Finished commands from other projects don't block."""
    ex = _make_executor(CommandStatus.COMPLETED)
    _active_commands["cmd-1"] = {
        "project_id": "proj-B",
        "executor": ex,
    }

    _reject_if_another_project_running("proj-A")


def test_no_rejection_when_no_commands():
    """No commands at all — no rejection."""
    _reject_if_another_project_running("proj-A")


# -- concurrent /run regression (the 2026-04-11 double-launch bug) -----------

@pytest.mark.asyncio
async def test_concurrent_run_requests_serialize_and_spawn_once(tmp_path, monkeypatch):
    """Regression: N concurrent POST /run/{project_id} must never spawn two
    overlapping subprocesses.

    Before the asyncio.Lock + eager registration fix, two near-simultaneous
    POSTs could both pass the "already running" check — the first request
    scheduled its executor task and registered in `_active_commands`, but the
    second request arrived during the FastAPI response-send yield, saw the
    PENDING entry, called `executor.cancel()` (a no-op while the subprocess
    had not yet spawned), and scheduled its OWN task. Both subprocesses then
    ran in parallel.

    This test installs a fake CommandExecutor that tracks peak simultaneous
    RUNNING count. After firing N concurrent `run_project` calls the peak
    must be ≤ 1.
    """
    from raccoon_cli.server.routes import commands as commands_module
    from raccoon_cli.server.services.executor import CommandStatus as ExecStatus

    project_path = tmp_path / "proj-A"
    project_path.mkdir()

    class FakeManager:
        def __init__(self, projects_dir):
            pass

        def get_project(self, pid):
            if pid != "proj-A":
                return None
            return {"id": pid, "name": pid, "path": project_path}

    class FakeConfig:
        projects_dir = tmp_path

    current_running = 0
    peak_running = 0
    executors_created: list = []

    class FakeExecutor:
        """Mimics CommandExecutor just enough for the scheduling path.

        Crucially, ``cancel()`` is a NO-OP while in PENDING state — exactly
        like the real CommandExecutor. This is what made the bug possible.
        """

        def __init__(self, buffer_size: int = 1000):
            self.status = ExecStatus.PENDING
            self.exit_code = None
            self.started_at = None
            self.finished_at = None
            self.output_line_count = 0
            self._hold = asyncio.Event()
            executors_created.append(self)

        async def execute(self, command_id, project_path, command, args, env=None):
            nonlocal current_running, peak_running
            self.status = ExecStatus.RUNNING
            current_running += 1
            peak_running = max(peak_running, current_running)
            try:
                # Hold the "subprocess" open until cancelled or until a
                # short grace window elapses. A well-behaved scheduler will
                # always cancel us (new request supersedes old) before the
                # timeout.
                try:
                    await asyncio.wait_for(self._hold.wait(), timeout=0.5)
                    self.status = ExecStatus.COMPLETED
                except asyncio.TimeoutError:
                    self.status = ExecStatus.COMPLETED
            except asyncio.CancelledError:
                self.status = ExecStatus.CANCELLED
                raise
            finally:
                current_running -= 1
            return 0

        async def cancel(self) -> None:
            # Mirrors the real CommandExecutor: a no-op while PENDING.
            # The task-based cancel path in _cancel_command_entry is what
            # actually has to work.
            if self.status == ExecStatus.RUNNING:
                self._hold.set()

    monkeypatch.setattr(
        "raccoon_cli.server.services.project_manager.ProjectManager", FakeManager
    )
    monkeypatch.setattr(
        "raccoon_cli.server.app.get_config", lambda: FakeConfig()
    )
    monkeypatch.setattr(
        "raccoon_cli.server.services.executor.CommandExecutor", FakeExecutor
    )

    commands_module._active_commands.clear()
    try:
        req = commands_module.CommandRequest()

        # Fire 10 concurrent run_project calls against the same project.
        results = await asyncio.gather(
            *[commands_module.run_project("proj-A", req) for _ in range(10)],
            return_exceptions=True,
        )

        # Every request must return a CommandResponse (each cancels its
        # predecessor); no request should have leaked an exception.
        for r in results:
            assert not isinstance(r, Exception), f"request raised: {r!r}"

        # Let the last surviving task enter execute() and run to completion.
        await asyncio.sleep(0.6)

        # Core invariant: never more than one subprocess running at a time.
        # In the buggy pre-fix code, all 10 tasks are created without
        # serialization and peak_running reaches ~10.
        assert peak_running <= 1, (
            f"peak concurrent runs = {peak_running}; "
            f"two instances of `raccoon run` would have driven the motors "
            f"simultaneously (this is the 2026-04-11 shaking bug)."
        )

        # At the end, at most one executor should be COMPLETED (the last
        # one); everything else must be CANCELLED. None should still be
        # RUNNING or PENDING.
        completed = [e for e in executors_created if e.status == ExecStatus.COMPLETED]
        cancelled = [e for e in executors_created if e.status == ExecStatus.CANCELLED]
        leaked = [
            e for e in executors_created
            if e.status in (ExecStatus.PENDING, ExecStatus.RUNNING)
        ]
        assert not leaked, f"executors left in non-terminal state: {leaked}"
        assert len(completed) <= 1, (
            f"{len(completed)} executors completed; only the last request "
            f"should survive, the rest should have been cancelled."
        )
        assert len(cancelled) >= len(executors_created) - 1
    finally:
        commands_module._active_commands.clear()


@pytest.mark.asyncio
async def test_concurrent_run_different_projects_rejects_second(tmp_path, monkeypatch):
    """Two concurrent POSTs for *different* projects: second must 409."""
    from fastapi import HTTPException

    from raccoon_cli.server.routes import commands as commands_module
    from raccoon_cli.server.services.executor import CommandStatus as ExecStatus

    (tmp_path / "proj-A").mkdir()
    (tmp_path / "proj-B").mkdir()

    class FakeManager:
        def __init__(self, projects_dir):
            pass

        def get_project(self, pid):
            return {"id": pid, "name": pid, "path": tmp_path / pid}

    class FakeConfig:
        projects_dir = tmp_path

    class FakeExecutor:
        def __init__(self, buffer_size: int = 1000):
            self.status = ExecStatus.PENDING
            self.exit_code = None
            self.started_at = None
            self.finished_at = None
            self.output_line_count = 0
            self._hold = asyncio.Event()

        async def execute(self, command_id, project_path, command, args, env=None):
            self.status = ExecStatus.RUNNING
            try:
                await self._hold.wait()
                self.status = ExecStatus.COMPLETED
            except asyncio.CancelledError:
                self.status = ExecStatus.CANCELLED
                raise
            return 0

        async def cancel(self):
            if self.status == ExecStatus.RUNNING:
                self._hold.set()

    monkeypatch.setattr(
        "raccoon_cli.server.services.project_manager.ProjectManager", FakeManager
    )
    monkeypatch.setattr(
        "raccoon_cli.server.app.get_config", lambda: FakeConfig()
    )
    monkeypatch.setattr(
        "raccoon_cli.server.services.executor.CommandExecutor", FakeExecutor
    )

    commands_module._active_commands.clear()
    try:
        req = commands_module.CommandRequest()

        results = await asyncio.gather(
            commands_module.run_project("proj-A", req),
            commands_module.run_project("proj-B", req),
            return_exceptions=True,
        )

        # Exactly one success, exactly one 409.
        successes = [r for r in results if not isinstance(r, Exception)]
        rejections = [
            r for r in results
            if isinstance(r, HTTPException) and r.status_code == 409
        ]
        assert len(successes) == 1, f"got {results!r}"
        assert len(rejections) == 1, f"got {results!r}"

        # Cancel the survivor so the test doesn't hang.
        for cmd in commands_module._active_commands.values():
            task = cmd.get("task")
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        commands_module._active_commands.clear()


@pytest.mark.asyncio
async def test_cancel_command_entry_cancels_pending_task():
    """The task-based cancel path must work even when executor is PENDING."""
    from raccoon_cli.server.routes.commands import _cancel_command_entry
    from raccoon_cli.server.services.executor import CommandStatus as ExecStatus

    class FakeExecutor:
        def __init__(self):
            self.status = ExecStatus.PENDING

        async def execute(self):
            self.status = ExecStatus.RUNNING
            await asyncio.sleep(10)  # would run forever
            return 0

        async def cancel(self):
            # No-op while PENDING (same as real executor) — so this test
            # would fail if _cancel_command_entry relied on executor.cancel().
            if self.status == ExecStatus.RUNNING:
                self.status = ExecStatus.CANCELLED

    executor = FakeExecutor()
    task = asyncio.create_task(executor.execute())
    cmd = {"project_id": "proj-A", "executor": executor, "task": task}

    await _cancel_command_entry(cmd)

    assert task.done()
    assert task.cancelled() or task.exception() is not None
    assert executor.status == ExecStatus.CANCELLED
