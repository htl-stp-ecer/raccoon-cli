""" Servo calibration routine API endpoints for starting and managing servo calibration """

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from raccoon_cli.server.auth import require_auth

logger = logging.getLogger("raccoon")
router = APIRouter(prefix="/api/v1/calibrate-servos", tags=["servo-calibration"], dependencies=[Depends(require_auth)])

_session: Optional[dict] = None  # { servo, servo_port, current_angle, initial_angle, transport, ui_task }
_session_lock = asyncio.Lock()


class ServoCalibrationStartRequest(BaseModel):
    """ Request to start a servo calibration session. """
    servo_id: str
    servo_port: int
    initial_angle: float

class ServoCalibrationMoveRequest(BaseModel):
    """ Request to move the servo during calibration. """
    delta_to_move: float

class ServoCalibrationEndResponse(BaseModel):
    """ The response model of the /end endpoint """
    initial_angle: float
    final_angle: float
    delta: float


@router.post("/start")
async def calibrate_servo_start(request: ServoCalibrationStartRequest):
    global _session

    async with _session_lock:
        if _session is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Session for '{request.servo_id}' already active. Call /end first."
            )

        try:
            global transport, servo

            from raccoon.hal import Servo
            from raccoon_transport import Transport
            transport = Transport()
            servo = Servo(port=request.servo_port)
            servo.enable()
            servo.set_position(request.initial_angle)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cancelling, failed to initialize servo: {e}")

        _session = {
            "servo": servo,
            "transport": transport,
            "current_angle": request.initial_angle,
            "initial_angle": request.initial_angle,
            "ui_task": asyncio.create_task(_servo_calibration_ui(request.servo_id, request.servo_port))
        }

    return {"status": "ok"}


@router.post("/move")
async def calibrate_servo_move(request: ServoCalibrationMoveRequest):
    global _session

    async with _session_lock:
        if _session is None:
            raise HTTPException(
                status_code=409,
                detail=f"No active calibration session, call /start first"
            )

        _session["current_angle"] += request.delta_to_move

        try:
            _session["servo"].set_position(_session["current_angle"])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to move servo: {e}")

    return {"status": "ok", "current_angle": _session["current_angle"]}


@router.post("/end", response_model=ServoCalibrationEndResponse)
async def calibrate_servo_end():
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

        _session["servo"].fully_disable_all()

        response = ServoCalibrationEndResponse(
            initial_angle=_session["initial_angle"],
            final_angle=_session["current_angle"],
            delta=_session["current_angle"] - _session["initial_angle"]
        )
        _session = None
        return response


async def _servo_calibration_ui(servo_id: str, servo_port: int):
    from raccoon.ui.step import UIStep
    from raccoon.ui.screen import UIScreen
    from raccoon.ui.widgets import Center, Text
    from raccoon_transport import Transport

    class ServoCalibrationScreen(UIScreen):
        title = "Servo Calibration"

        def build(self):
            return Center(children=[
                Text(f"Calibrating {servo_id} on port {servo_port}", size="large"),
                Text("Use your laptop to calibrate", size="medium"),
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