"""LCM spy and playback API endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon_cli.server.auth import require_auth

router = APIRouter(
    prefix="/api/v1/lcm", tags=["lcm"], dependencies=[Depends(require_auth)]
)


class SpyStartRequest(BaseModel):
    """Request to start LCM spy."""

    channel_patterns: list[str] = []  # Empty = all channels
    record_to: Optional[str] = None  # Optional filename for recording


class SpyStatusResponse(BaseModel):
    """Response for spy status."""

    status: str
    message_count: int
    channels_seen: list[str]
    channel_patterns: list[str]
    start_time: Optional[str] = None
    recording_file: Optional[str] = None
    error: Optional[str] = None


class SpyStopResponse(BaseModel):
    """Response from stopping spy."""

    status: str
    message_count: int
    channels_seen: list[str]
    recording_file: Optional[str] = None


class PlaybackStartRequest(BaseModel):
    """Request to start playback."""

    filename: str
    speed: float = 1.0
    loop: bool = False
    channel_filter: list[str] = []


class PlaybackStatusResponse(BaseModel):
    """Response for playback status."""

    status: str
    filename: Optional[str] = None
    messages_played: int = 0
    total_messages: int = 0
    speed: float = 1.0
    loop: bool = False
    error: Optional[str] = None


class RecordingInfo(BaseModel):
    """Information about a recording."""

    filename: str
    size_bytes: int
    message_count: int
    created_at: str
    modified_at: str


def _get_recordings_dir() -> Path:
    """Get the LCM recordings directory."""
    from raccoon_cli.server.app import get_config

    config = get_config()
    return Path.home() / ".raccoon" / "lcm_recordings"


def _get_spy_service():
    """Get the LCM spy service instance."""
    from raccoon_cli.server.services.lcm_spy import get_spy_service

    return get_spy_service(_get_recordings_dir())


def _get_playback_service():
    """Get the LCM playback service instance."""
    from raccoon_cli.server.services.lcm_spy import get_playback_service

    return get_playback_service(_get_recordings_dir())


@router.post("/spy/start")
async def start_spy(request: SpyStartRequest):
    """
    Start spying on LCM channels.

    Connect to WebSocket at /ws/lcm to receive live messages.
    """
    service = _get_spy_service()
    result = service.start(
        channel_patterns=request.channel_patterns or None,
        record_to=request.record_to,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    result["websocket_url"] = "/ws/lcm"
    return result


@router.post("/spy/stop", response_model=SpyStopResponse)
async def stop_spy():
    """Stop the current spy session."""
    service = _get_spy_service()
    return service.stop()


@router.get("/spy/status", response_model=SpyStatusResponse)
async def get_spy_status():
    """Get current spy session status and statistics."""
    service = _get_spy_service()
    return service.stats


@router.get("/recordings", response_model=list[RecordingInfo])
async def list_recordings():
    """List available LCM recordings."""
    service = _get_playback_service()
    return service.list_recordings()


@router.delete("/recordings/{filename}")
async def delete_recording(filename: str):
    """Delete a recording file."""
    service = _get_playback_service()
    if not service.delete_recording(filename):
        raise HTTPException(status_code=404, detail=f"Recording not found: {filename}")
    return {"status": "deleted", "filename": filename}


@router.post("/playback/start")
async def start_playback(request: PlaybackStartRequest):
    """Start playback of a recorded LCM session."""
    service = _get_playback_service()
    result = service.start_playback(
        filename=request.filename,
        speed=request.speed,
        loop=request.loop,
        channel_filter=request.channel_filter or None,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/playback/stop")
async def stop_playback():
    """Stop current playback."""
    service = _get_playback_service()
    return service.stop_playback()


@router.get("/playback/status", response_model=PlaybackStatusResponse)
async def get_playback_status():
    """Get current playback status and progress."""
    service = _get_playback_service()
    return {
        "status": service.status.value,
        **service.progress,
    }


@router.get("/info")
async def get_lcm_info():
    """Get information about LCM spy capabilities."""
    from raccoon_cli.server.services.lcm_spy import LCM_AVAILABLE, EXLCM_TYPES

    return {
        "lcm_available": LCM_AVAILABLE,
        "decoding_available": bool(EXLCM_TYPES),
        "known_types": list(EXLCM_TYPES.keys()),
        "recordings_dir": str(_get_recordings_dir()),
    }


class ServiceControlRequest(BaseModel):
    """Request to control a systemd service."""
    service_name: str
    action: str  # start, stop, restart


@router.post("/service/control")
async def control_service(request: ServiceControlRequest):
    """Control a systemd service (start/stop/restart)."""
    import subprocess

    allowed_services = ["stm32_data_reader.service", "stm32_data_reader"]
    allowed_actions = ["start", "stop", "restart", "status"]

    if request.service_name not in allowed_services:
        raise HTTPException(
            status_code=400,
            detail=f"Service not allowed. Allowed: {allowed_services}"
        )

    if request.action not in allowed_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Action not allowed. Allowed: {allowed_actions}"
        )

    service = request.service_name
    if not service.endswith(".service"):
        service = f"{service}.service"

    try:
        if request.action == "status":
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True,
                text=True,
            )
            return {
                "service": service,
                "status": result.stdout.strip(),
                "active": result.returncode == 0,
            }
        else:
            result = subprocess.run(
                ["sudo", "systemctl", request.action, service],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return {
                    "service": service,
                    "action": request.action,
                    "success": False,
                    "error": result.stderr.strip(),
                }
            return {
                "service": service,
                "action": request.action,
                "success": True,
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
