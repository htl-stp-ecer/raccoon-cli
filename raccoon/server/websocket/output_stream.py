"""WebSocket handler for streaming command output."""

import asyncio
from typing import Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from raccoon.server.config import get_or_create_api_token
from raccoon.server.services.executor import CommandStatus

# Reference to active commands (shared with commands router)
from raccoon.server.routes.commands import _active_commands


def setup_websocket_routes(app: FastAPI) -> None:
    """Set up WebSocket routes on the FastAPI app."""

    @app.websocket("/ws/output/{command_id}")
    async def websocket_output(
        websocket: WebSocket,
        command_id: str,
        token: str = Query(default=""),
    ):
        """
        WebSocket endpoint for streaming command output.

        Clients connect here to receive real-time output from
        running commands. The connection stays open until the
        command completes or the client disconnects.

        Authentication is required via the 'token' query parameter.

        Protocol:
        - Server sends text messages, one per line of output
        - Server sends JSON {"status": "completed", "exit_code": N} when done
        - Client can send {"action": "cancel"} to cancel the command
        """
        # Verify token before accepting connection
        expected_token = get_or_create_api_token()
        if token != expected_token:
            await websocket.close(code=4001, reason="Invalid or missing API token")
            return

        await websocket.accept()

        # Find the command
        if command_id not in _active_commands:
            await websocket.send_json(
                {"error": "Command not found", "command_id": command_id}
            )
            await websocket.close()
            return

        cmd = _active_commands[command_id]
        executor = cmd.get("executor")

        if not executor:
            await websocket.send_json(
                {"error": "Command executor not available", "command_id": command_id}
            )
            await websocket.close()
            return

        # Subscribe to output stream
        output_queue = executor.subscribe()

        try:
            # Handle bidirectional communication
            receive_task = asyncio.create_task(_receive_messages(websocket, executor))
            send_task = asyncio.create_task(
                _send_output(websocket, output_queue, executor)
            )

            # Wait for either task to complete
            done, pending = await asyncio.wait(
                [receive_task, send_task], return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except WebSocketDisconnect:
            # Client disconnected - cancel the running process
            if executor.status == CommandStatus.RUNNING:
                await executor.cancel()

        finally:
            executor.unsubscribe(output_queue)


async def _receive_messages(websocket: WebSocket, executor) -> None:
    """Handle incoming WebSocket messages from client."""
    try:
        while True:
            data = await websocket.receive_json()

            if data.get("action") == "cancel":
                await executor.cancel()
                await websocket.send_json({"status": "cancelling"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


async def _send_output(
    websocket: WebSocket, output_queue: asyncio.Queue, executor
) -> None:
    """Send output lines to the WebSocket client."""
    try:
        while True:
            line = await output_queue.get()

            if line is None:
                # End of output - send final status
                await websocket.send_json(
                    {
                        "status": str(executor.status.value),
                        "exit_code": executor.exit_code,
                        "finished_at": executor.finished_at,
                    }
                )
                break

            # Send output line
            await websocket.send_text(line)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
