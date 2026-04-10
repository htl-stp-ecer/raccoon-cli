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
