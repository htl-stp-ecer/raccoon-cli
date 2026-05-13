"""Simple direct servo command endpoint — no project context needed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon_cli.server.auth import require_auth

router = APIRouter(
    prefix="/api/v1/servo",
    tags=["servo"],
    dependencies=[Depends(require_auth)],
)


class ServoPosition(BaseModel):
    port: int
    angle_deg: float


class ServoSetRequest(BaseModel):
    positions: list[ServoPosition]


class ServoSetResponse(BaseModel):
    success: bool
    count: int


@router.post("/set", response_model=ServoSetResponse)
async def set_servo_positions(request: ServoSetRequest):
    """Set one or more servos by port and angle. No project config required."""
    try:
        from raccoon.hal import Servo  # type: ignore  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="raccoon.hal not available — hardware access requires running on the Pi.",
        ) from exc

    for pos in request.positions:
        Servo(port=pos.port).set_position(float(pos.angle_deg))

    return ServoSetResponse(success=True, count=len(request.positions))
