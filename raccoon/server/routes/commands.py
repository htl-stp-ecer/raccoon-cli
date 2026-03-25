"""Command execution endpoints."""

import asyncio
import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from raccoon.server.auth import require_auth

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


async def _cancel_running_commands_for_project(project_id: str) -> None:
    """Cancel any running commands for the given project.

    Ensures only one program runs per project at a time.  When a new
    run/calibrate is requested the previous one is terminated first.
    """
    from raccoon.server.services.executor import CommandStatus

    for cmd_id, cmd in list(_active_commands.items()):
        if cmd["project_id"] != project_id:
            continue
        executor = cmd.get("executor")
        if executor and executor.status in (CommandStatus.PENDING, CommandStatus.RUNNING):
            logger.info(
                "Cancelling previous command %s for project %s", cmd_id, project_id
            )
            await executor.cancel()


def _reject_if_another_project_running(project_id: str) -> None:
    """Raise 409 if a *different* project has a running command.

    Only one program may execute on the robot at a time.  Same-project
    commands are already cancelled by ``_cancel_running_commands_for_project``,
    but two different projects must not overlap.
    """
    from raccoon.server.services.executor import CommandStatus

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


@router.post("/run/{project_id}", response_model=CommandResponse)
async def run_project(project_id: str, request: CommandRequest = CommandRequest()):
    """
    Start running a project.

    Executes `raccoon run` in the project directory.
    Returns a command ID and WebSocket URL for output streaming.
    """
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager
    from raccoon.server.services.executor import CommandExecutor

    config = get_config()
    manager = ProjectManager(config.projects_dir)

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

    # Start the command asynchronously
    # Use --local to force local execution (we're already on the Pi)
    # Use --no-codegen because codegen is done client-side before sync
    asyncio.create_task(
        executor.execute(
            command_id=command_id,
            project_path=project["path"],
            command="raccoon",
            args=["run", "--local", "--no-codegen"] + request.args,
            env=request.env,
        )
    )

    _active_commands[command_id] = {
        "status": CommandStatus.PENDING,
        "project_id": project_id,
        "command_type": CommandType.RUN,
        "started_at": datetime.utcnow().isoformat(),
        "executor": executor,
    }

    return CommandResponse(
        command_id=command_id,
        status=CommandStatus.PENDING,
        project_id=project_id,
        command_type=CommandType.RUN,
        started_at=_active_commands[command_id]["started_at"],
        websocket_url=f"/ws/output/{command_id}",
    )


@router.post("/calibrate/{project_id}", response_model=CommandResponse)
async def calibrate_project(
    project_id: str, request: CommandRequest = CommandRequest()
):
    """
    Start motor calibration for a project.

    Executes `raccoon calibrate` in the project directory.
    """
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager
    from raccoon.server.services.executor import CommandExecutor

    config = get_config()
    manager = ProjectManager(config.projects_dir)

    project = manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    # Only one program may run on the robot at a time.
    await _cancel_running_commands_for_project(project_id)
    _reject_if_another_project_running(project_id)

    command_id = str(uuid.uuid4())
    executor = CommandExecutor()

    # Start the command asynchronously
    # Use --local to force local execution (we're already on the Pi)
    # Note: --local must come AFTER the subcommand (e.g., "calibrate motors --local")
    asyncio.create_task(
        executor.execute(
            command_id=command_id,
            project_path=project["path"],
            command="raccoon",
            args=["calibrate"] + request.args + ["--local"],
            env=request.env,
        )
    )

    _active_commands[command_id] = {
        "status": CommandStatus.PENDING,
        "project_id": project_id,
        "command_type": CommandType.CALIBRATE,
        "started_at": datetime.utcnow().isoformat(),
        "executor": executor,
    }

    return CommandResponse(
        command_id=command_id,
        status=CommandStatus.PENDING,
        project_id=project_id,
        command_type=CommandType.CALIBRATE,
        started_at=_active_commands[command_id]["started_at"],
        websocket_url=f"/ws/output/{command_id}",
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


@router.post("/commands/{command_id}/cancel")
async def cancel_command(command_id: str):
    """
    Cancel a running command.
    """
    if command_id not in _active_commands:
        raise HTTPException(
            status_code=404, detail=f"Command '{command_id}' not found"
        )

    cmd = _active_commands[command_id]
    executor = cmd.get("executor")

    if executor:
        await executor.cancel()

    return {"status": "cancelled", "command_id": command_id}
