"""WebSocket handler for streaming LCM messages."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from raccoon.server.config import get_or_create_api_token


def _get_recordings_dir() -> Path:
    """Get the LCM recordings directory."""
    return Path.home() / ".raccoon" / "lcm_recordings"


def setup_lcm_websocket(app: FastAPI) -> None:
    """Set up LCM WebSocket route on the FastAPI app."""

    @app.websocket("/ws/lcm")
    async def websocket_lcm(
        websocket: WebSocket,
        token: str = Query(default=""),
    ):
        """
        WebSocket endpoint for streaming LCM messages.

        Clients connect here to receive real-time LCM messages
        captured by the spy service.

        Authentication is required via the 'token' query parameter.

        Protocol:
        - Server sends JSON messages: {"type": "message", "channel": "...", ...}
        - Server sends {"type": "status", "status": "stopped", ...} when spy stops
        - Server sends {"type": "error", "error": "..."} on errors
        - Client can send {"action": "stop"} to stop the spy
        """
        # Verify token before accepting connection
        expected_token = get_or_create_api_token()
        if token != expected_token:
            await websocket.close(code=4001, reason="Invalid or missing API token")
            return

        await websocket.accept()

        # Get spy service
        from raccoon.server.services.lcm_spy import get_spy_service

        service = get_spy_service(_get_recordings_dir())

        if not service.is_running:
            await websocket.send_json(
                {
                    "type": "error",
                    "error": "Spy not running. Start with POST /api/v1/lcm/spy/start",
                }
            )
            await websocket.close()
            return

        # Subscribe to message stream
        message_queue = service.subscribe()

        try:
            # Handle bidirectional communication
            receive_task = asyncio.create_task(
                _receive_client_messages(websocket, service)
            )
            send_task = asyncio.create_task(
                _send_lcm_messages(websocket, message_queue, service)
            )

            # Wait for either task to complete
            done, pending = await asyncio.wait(
                [receive_task, send_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except WebSocketDisconnect:
            pass

        finally:
            service.unsubscribe(message_queue)


async def _receive_client_messages(websocket: WebSocket, service) -> None:
    """Handle incoming WebSocket messages from client."""
    try:
        while True:
            data = await websocket.receive_json()

            if data.get("action") == "stop":
                service.stop()
                await websocket.send_json({"type": "status", "status": "stopping"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


async def _send_lcm_messages(
    websocket: WebSocket, queue: asyncio.Queue, service
) -> None:
    """Send LCM messages to WebSocket client."""
    try:
        while service.is_running:
            try:
                # Use timeout to periodically check if service stopped
                msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                continue

        # Send final status when spy stops
        await websocket.send_json(
            {
                "type": "status",
                "status": "stopped",
                **service.stats,
            }
        )

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
