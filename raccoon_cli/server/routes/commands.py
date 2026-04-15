"""Command execution endpoints."""

import asyncio
import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from raccoon_cli.server.auth import require_auth

logger = logging.getLogger("raccoon")

router = APIRouter(prefix="/api/v1", tags=["commands"], dependencies=[Depends(require_auth)])


class CommandType(str, Enum):
    """Available command types."""

    RUN = "run"
    CALIBRATE = "calibrate"


class CommandStatus(str, Enum):
    """Command execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommandRequest(BaseModel):
    """Request model for command execution."""

    args: list[str] = []
    env: dict[str, str] = {}


class CommandResponse(BaseModel):
    """Response model for command execution."""

    command_id: str
    status: CommandStatus
    project_id: str
    command_type: CommandType
    started_at: str
    websocket_url: str


class CommandStatusResponse(BaseModel):
    """Response model for command status."""

    command_id: str
    status: CommandStatus
    exit_code: Optional[int] = None
    started_at: str
    finished_at: Optional[str] = None
    output_lines: int = 0


# In-memory command tracking (in production, use Redis or similar)
_active_commands: dict[str, dict] = {}

# Serializes the "cancel-previous / reject-other / register-new" critical
# section in _start_command. Without this, two near-simultaneous POSTs can
# both pass the "already running" check (because neither has registered
# yet) and both spawn a `raccoon run` subprocess — causing two robot
# programs to drive the motors at the same time. Observed on 2026-04-11:
# two POSTs /api/v1/run/<id> arrived within the same second and both
# subprocesses ran, producing duplicated libstp.log entries and a shaking
# bot. See also test_concurrent_run_requests_serialize_and_spawn_once.
_command_lock = asyncio.Lock()


async def _cancel_command_entry(cmd: dict) -> None:
    """Cancel an active command entry, guaranteeing the subprocess is dead.

    Uses the stored asyncio Task handle when available, because
    ``executor.cancel()`` is a no-op when the executor is still PENDING
    (subprocess not yet spawned). Cancelling the Task works in both
    PENDING and RUNNING states: if the task has not started yet, it is
    cancelled before executor.execute() ever runs; if it is running, the
    CancelledError propagates into execute() which SIGTERMs the
    subprocess and awaits its exit.
    """
    from raccoon_cli.server.services.executor import CommandStatus as ExecStatus

    executor = cmd.get("executor")
    task: Optional[asyncio.Task] = cmd.get("task")

    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    elif executor is not None:
        await executor.cancel()

    if executor is not None and executor.status in (ExecStatus.PENDING, ExecStatus.RUNNING):
        executor.status = ExecStatus.CANCELLED


async def _cancel_running_commands_for_project(project_id: str) -> None:
    """Cancel any running commands for the given project.

    Ensures only one program runs per project at a time.  When a new
    run/calibrate is requested the previous one is terminated first.
    """
    from raccoon_cli.server.services.executor import CommandStatus as ExecStatus

    for cmd_id, cmd in list(_active_commands.items()):
        if cmd["project_id"] != project_id:
            continue
        executor = cmd.get("executor")
        if executor and executor.status not in (ExecStatus.PENDING, ExecStatus.RUNNING):
            continue
        logger.info(
            "Cancelling previous command %s for project %s", cmd_id, project_id
        )
        await _cancel_command_entry(cmd)


def _reject_if_another_project_running(project_id: str) -> None:
    """Raise 409 if a *different* project has a running command.

    Only one program may execute on the robot at a time.  Same-project
    commands are already cancelled by ``_cancel_running_commands_for_project``,
    but two different projects must not overlap.
    """
    from raccoon_cli.server.services.executor import CommandStatus

    for cmd_id, cmd in _active_commands.items():
        if cmd["project_id"] == project_id:
            continue
        executor = cmd.get("executor")
        if executor and executor.status in (CommandStatus.PENDING, CommandStatus.RUNNING):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Another project ({cmd['project_id']}) is already running "
                    f"(command {cmd_id}). Stop it first or wait for it to finish."
                ),
            )


async def _start_command(
    project_id: str,
    command_type: CommandType,
    args: list[str],
    env: dict[str, str],
) -> CommandResponse:
    """Schedule a new command after cancelling any same-project predecessor.

    The entire critical section runs under ``_command_lock`` so that two
    concurrent POSTs cannot both pass the "already running" check and
    both spawn a subprocess. The new command is registered in
    ``_active_commands`` BEFORE the lock is released — any waiter will
    observe it and take the cancel-or-reject path instead of spawning
    another subprocess.
    """
    from raccoon_cli.server.app import get_config
    from raccoon_cli.server.services.project_manager import ProjectManager
    from raccoon_cli.server.services.executor import CommandExecutor

    config = get_config()
    manager = ProjectManager(config.projects_dir)

    async with _command_lock:
        project = manager.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

        # Only one program may run on the robot at a time.
        # Cancel any previous command for this project AND reject if *any other*
        # project still has a running command (same physical robot).
        await _cancel_running_commands_for_project(project_id)
        _reject_if_another_project_running(project_id)

        command_id = str(uuid.uuid4())
        executor = CommandExecutor()

        task = asyncio.create_task(
            executor.execute(
                command_id=command_id,
                project_path=project["path"],
                command="raccoon",
                args=args,
                env=env,
            )
        )

        # Eager registration MUST happen before releasing the lock so that
        # the next waiter sees this entry. See _command_lock docstring.
        _active_commands[command_id] = {
            "status": CommandStatus.PENDING,
            "project_id": project_id,
            "command_type": command_type,
            "started_at": datetime.utcnow().isoformat(),
            "executor": executor,
            "task": task,
        }

        return CommandResponse(
            command_id=command_id,
            status=CommandStatus.PENDING,
            project_id=project_id,
            command_type=command_type,
            started_at=_active_commands[command_id]["started_at"],
            websocket_url=f"/ws/output/{command_id}",
        )


@router.post("/run/{project_id}", response_model=CommandResponse)
async def run_project(project_id: str, request: CommandRequest = CommandRequest()):
    """
    Start running a project.

    Executes `raccoon run` in the project directory.
    Returns a command ID and WebSocket URL for output streaming.
    """
    # Use --local to force local execution (we're already on the Pi)
    # Use --no-codegen because codegen is done client-side before sync
    return await _start_command(
        project_id=project_id,
        command_type=CommandType.RUN,
        args=["run", "--local", "--no-codegen", *request.args],
        env=request.env,
    )


@router.post("/calibrate/{project_id}", response_model=CommandResponse)
async def calibrate_project(
    project_id: str, request: CommandRequest = CommandRequest()
):
    """
    Start motor calibration for a project.

    Executes `raccoon calibrate` in the project directory.
    """
    return await _start_command(
        project_id=project_id,
        command_type=CommandType.CALIBRATE,
        args=["calibrate", *request.args, "--local"],
        env=request.env,
    )


@router.get("/commands/{command_id}/status", response_model=CommandStatusResponse)
async def get_command_status(command_id: str):
    """
    Get the status of a running or completed command.
    """
    if command_id not in _active_commands:
        raise HTTPException(
            status_code=404, detail=f"Command '{command_id}' not found"
        )

    cmd = _active_commands[command_id]
    executor = cmd.get("executor")

    return CommandStatusResponse(
        command_id=command_id,
        status=executor.status if executor else cmd["status"],
        exit_code=executor.exit_code if executor else None,
        started_at=cmd["started_at"],
        finished_at=executor.finished_at if executor else None,
        output_lines=executor.output_line_count if executor else 0,
    )


class RunningCommandResponse(BaseModel):
    """Currently active command, if any."""

    is_running: bool
    command_id: Optional[str] = None
    project_id: Optional[str] = None
    command_type: Optional[CommandType] = None
    started_at: Optional[str] = None


@router.get("/commands/running", response_model=RunningCommandResponse)
async def get_running_command():
    """
    Return the currently running (or pending) command, if any.
    Returns is_running=false when the robot is idle.
    """
    from raccoon_cli.server.services.executor import CommandStatus as ExecStatus

    for cmd_id, cmd in _active_commands.items():
        executor = cmd.get("executor")
        if executor and executor.status in (ExecStatus.PENDING, ExecStatus.RUNNING):
            return RunningCommandResponse(
                is_running=True,
                command_id=cmd_id,
                project_id=cmd["project_id"],
                command_type=cmd["command_type"],
                started_at=cmd["started_at"],
            )

    return RunningCommandResponse(is_running=False)


@router.post("/commands/{command_id}/cancel")
async def cancel_command(command_id: str):
    """
    Cancel a running command.
    """
    if command_id not in _active_commands:
        raise HTTPException(
            status_code=404, detail=f"Command '{command_id}' not found"
        )

    await _cancel_command_entry(_active_commands[command_id])

    return {"status": "cancelled", "command_id": command_id}
