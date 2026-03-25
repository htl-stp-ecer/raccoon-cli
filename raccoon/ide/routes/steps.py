"""Step discovery API routes."""

import asyncio
import logging
import httpx
from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field

from raccoon.ide.services.step_discovery_service import StepDiscoveryService

router = APIRouter()
logger = logging.getLogger(__name__)


class StepIndexPayload(BaseModel):
    """Request body for importing a step index produced by another backend."""

    steps: List[Dict[str, Any]] = Field(default_factory=list)
    last_indexed_at: str | None = None


def get_step_discovery_service() -> StepDiscoveryService:
    """Dependency injection for StepDiscoveryService - will be overridden by app."""
    raise NotImplementedError("StepDiscoveryService dependency not configured")


@router.get("/", response_model=List[Dict[str, Any]])
async def get_available_steps(
        project_uuid: UUID = Query(None, description="Project UUID to include project-specific steps"),
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> List[Dict[str, Any]]:
    """Return cached library steps plus project-local steps when requested."""
    return await asyncio.to_thread(svc.get_all_available_steps, project_uuid)


@router.get("/index/status")
async def get_step_index_status(
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Return the current libstp step-cache status."""
    return svc.get_libstp_cache_status()


@router.post("/index/refresh")
async def refresh_step_index(
        device_url: str | None = Query(None, description="Device backend URL (e.g., http://192.168.4.1:8000)"),
        force_clear: bool = Query(False, description="Clear cache before indexing"),
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Refresh the libstp step cache from a device backend or the local install."""
    logger.info(f"POST /index/refresh called with device_url={device_url}, force_clear={force_clear}")

    if force_clear:
        logger.info("Clearing local cache")
        svc.clear_libstp_cache()

    if not device_url:
        logger.info("Refreshing step cache from local libstp installation")
        try:
            return await asyncio.to_thread(svc.refresh_libstp_cache_locally)
        except RuntimeError as e:
            logger.error(f"Local indexing failed: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    # Normalize device URL
    base_url = device_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"

    fetch_url = f"{base_url}/api/v1/steps"
    logger.info(f"Fetching steps from device: {fetch_url}")

    # Fetch steps from device backend
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(fetch_url)
            response.raise_for_status()
            steps = response.json()
            logger.info(f"Received {len(steps)} steps from device")
    except httpx.RequestError as e:
        logger.warning(f"Failed to connect to device: {e}")
        logger.info("Falling back to local libstp indexing")
        try:
            return await asyncio.to_thread(svc.refresh_libstp_cache_locally)
        except RuntimeError as local_error:
            logger.error(f"Local indexing failed after device fallback: {local_error}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to connect to device: {e}; local fallback failed: {local_error}",
            )
    except httpx.HTTPStatusError as e:
        logger.error(f"Device returned error: {e.response.status_code}")
        raise HTTPException(status_code=502, detail=f"Device returned error: {e.response.status_code}")

    # Import steps into local cache
    logger.info(f"Importing {len(steps)} steps into local cache")
    svc.import_libstp_cache(steps)
    status = svc.get_libstp_cache_status()
    logger.info(f"Cache status after import: {status}")
    return status


@router.post("/index/clear")
async def clear_step_index(
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Clear the cached libstp step index."""
    svc.clear_libstp_cache()
    return svc.get_libstp_cache_status()


@router.post("/index/import")
async def import_step_index(
        payload: StepIndexPayload,
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Import a previously generated step index into the local cache."""
    svc.import_libstp_cache(payload.steps, payload.last_indexed_at)
    return svc.get_libstp_cache_status()
