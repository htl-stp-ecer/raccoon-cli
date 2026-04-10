"""Hardware interaction endpoints for direct motor/sensor access."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon_cli.server.auth import require_auth

router = APIRouter(prefix="/api/v1/hardware", tags=["hardware"], dependencies=[Depends(require_auth)])


class EncoderReadRequest(BaseModel):
    """Request model for reading encoder position."""

    port: int
    inverted: bool = False


class EncoderReadResponse(BaseModel):
    """Response model for encoder position."""

    port: int
    position: int
    success: bool
    error: Optional[str] = None


@router.post("/encoder/read", response_model=EncoderReadResponse)
async def read_encoder_position(request: EncoderReadRequest):
    """
    Read the current encoder position for a motor.

    This provides direct hardware access for calibration purposes.
    """
    try:
        from libstp.hal import Motor as HalMotor  # type: ignore

        motor = HalMotor(port=request.port, inverted=request.inverted)
        position = motor.get_position()

        return EncoderReadResponse(
            port=request.port,
            position=position,
            success=True,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="libstp not available on this system. Hardware access requires running on the Pi.",
        ) from e
    except Exception as e:
        return EncoderReadResponse(
            port=request.port,
            position=0,
            success=False,
            error=str(e),
        )
