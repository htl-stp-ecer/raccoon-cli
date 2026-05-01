""" Servo calibration routine API endpoints for starting and managing servo calibration """

import asyncio
import logging
import sys
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from raccoon_cli.server.auth import require_auth
from raccoon.ui.screen import UIScreen
from raccoon.ui.widgets import Center, Text

logger = logging.getLogger("raccoon")
router = APIRouter(prefix="/api/v1", tags=["servo-calibration"], dependencies=[Depends(require_auth)])

_session: Optional[dict] = None  # { port, position_deg, initial_deg,  ui_task }
_session_lock = asyncio.Lock()

class ServoCalibrationResponse(BaseModel):
    """ The response model of the /end endpoint """
    initial_deg: float
    final_deg: float
    delta_deg: float


@router.post("/calibrate-servo/start")
async def calibrate_servo_start(
    servo_id: str, port: int, initial_angle: float
):
    """
    Start a servo calibration session.
    Creates a blank UI and moves the servo to the given position
    """
    global _session

    async with _session_lock:
        if _session is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Session for '{servo_id}' already active. Call /end first."
            )

        try:
            from raccoon.hal import Servo
            Servo(port=port).set_angle(initial_angle)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to move servo: {e}")

        _session = {
            "port": port,
            "position_deg": initial_angle,
            "initial_deg": initial_angle,
            "ui_task": asyncio.create_task(_servo_calibration_ui(servo_id, port))
        }


@router.post("/calibrate-servo/move")
async def calibrate_servo_move(
    angle: float
):
    """
    Move a servo relative to its current position for calibration.
    Only works when a calibration session is active.
    """
    global _session

    async with _session_lock:
        if _session is None:
            raise HTTPException(
                status_code=409,
                detail=f"No active calibration session, call /start first"
            )

        _session["position_deg"] += angle

        try:
            from raccoon.hal import Servo
            Servo(port=_session["port"]).set_angle(_session["position_deg"])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to move servo: {e}")


@router.post("/calibrate-servo/end", response_model=ServoCalibrationResponse)
async def calibrate_servo_end():
    """
    Stop a servo calibration session.
    """
    global _session

    async with _session_lock:
        if _session is None:
            raise HTTPException(
                status_code=409,
                detail=f"No active calibration session, call /start first"
            )

        ui_task = _session["ui_task"]
        if not ui_task.done():
            ui_task.cancel()
            try:
                await ui_task
            except (asyncio.CancelledError, Exception):
                pass

        response = ServoCalibrationResponse(
            initial_deg=_session["initial_deg"],
            final_deg=_session["position_deg"],
            delta_deg=_session["position_deg"] - _session["initial_deg"]
        )
        _session = None
        return response


async def _servo_calibration_ui(servo_id: str, port: int):
    """ Servo Calibration UI Lock """

    from raccoon.ui.step import UIStep
    from raccoon.ui.screen import UIScreen
    from raccoon.ui.widgets import Center, Text
    from raccoon_transport import Transport

    class ServoCalibrationScreen(UIScreen):
        title = "Servo Calibration"

        def build(self):
            return Center(children=[
                Text(f"Calibrating {servo_id} on port {port}...", size="large"),
            ])

    step = UIStep.__new__(UIStep)
    step._transport = Transport()
    step._current_screen = None
    step._ui_active = False
    step._pump_queue = None
    step._pump_sub = None

    await step.display(ServoCalibrationScreen())

    try:
        while True:
            await step.pump_events()
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        await step.close_ui()