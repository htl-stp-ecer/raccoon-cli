"""Step discovery API routes."""

from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from raccoon.ide.services.step_discovery_service import StepDiscoveryService

router = APIRouter()


def get_step_discovery_service() -> StepDiscoveryService:
    """Dependency injection for StepDiscoveryService - will be overridden by app."""
    raise NotImplementedError("StepDiscoveryService dependency not configured")


@router.get("/", response_model=List[Dict[str, Any]])
async def get_available_steps(
        project_uuid: UUID = Query(None, description="Project UUID to include project-specific steps"),
        svc: StepDiscoveryService = Depends(get_step_discovery_service),
) -> List[Dict[str, Any]]:
    return svc.get_all_available_steps(project_uuid)
