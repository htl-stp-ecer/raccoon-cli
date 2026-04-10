"""Step discovery API routes."""

import asyncio
import logging
from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from raccoon_cli.ide.services.step_discovery_service import StepDiscoveryService

router = APIRouter()
logger = logging.getLogger(__name__)


def get_step_discovery_service() -> StepDiscoveryService:
    """Dependency injection for StepDiscoveryService - will be overridden by app."""
    raise NotImplementedError("StepDiscoveryService dependency not configured")


@router.get("/", response_model=List[Dict[str, Any]])
async def get_available_steps(
        project_uuid: UUID = Query(None, description="Project UUID to include project-specific steps"),
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> List[Dict[str, Any]]:
    """Return all available steps (library + project-local)."""
    return await asyncio.to_thread(svc.get_all_available_steps, project_uuid)


@router.get("/index/status")
async def get_step_index_status(
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Return library step discovery status."""
    return svc.get_raccoon_cache_status()


@router.post("/index/refresh")
async def refresh_step_index(
        force_clear: bool = Query(False, description="Clear in-memory cache before re-scanning"),
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Re-scan the local raccoon installation for steps."""
    if force_clear:
        svc.clear_raccoon_cache()
    return await asyncio.to_thread(svc.refresh_raccoon_cache_locally)


@router.post("/index/clear")
async def clear_step_index(
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> Dict[str, Any]:
    """Clear the in-memory step cache."""
    svc.clear_raccoon_cache()
    return svc.get_raccoon_cache_status()
