""" Servo calibration routine API endpoints for starting and managing servo calibration """

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from raccoon_cli.server.auth import require_auth

logger = logging.getLogger("raccoon")
router = APIRouter(prefix="/api/v1/calibrate-servos", tags=["servo-calibration"], dependencies=[Depends(require_auth)])

_session: Optional[dict] = None  # { servo, servo_port, current_angle, initial_angle }
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
            from raccoon.hal import Motor
            from raccoon.hal import Servo
            Motor.enable_all()
            servo = Servo(port=request.servo_port)
            servo.enable()
            servo.set_position(float(request.initial_angle))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cancelling, failed to initialize servo: {e}")

        _session = {
            "servo": servo,
            "servo_port": request.servo_port,
            "current_angle": request.initial_angle,
            "initial_angle": request.initial_angle,
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
            from raccoon.hal import Motor

            Motor.enable_all()
            _session["servo"].enable()
            _session["servo"].set_position(float(_session["current_angle"]))
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

        try:
            _session["servo"].fully_disable_all()
        except Exception as exc:
            logger.warning("Failed to fully disable servo after calibration: %s", exc)

        response = ServoCalibrationEndResponse(
            initial_angle=_session["initial_angle"],
            final_angle=_session["current_angle"],
            delta=_session["current_angle"] - _session["initial_angle"]
        )
        _session = None
        return response
