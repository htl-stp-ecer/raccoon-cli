"""Type definition API routes."""

from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter

router = APIRouter()


@router.get("/{project_uuid}", response_model=List[Dict[str, Any]])
async def get_type_definitions(project_uuid: UUID) -> List[Dict[str, Any]]:
    """Return type definitions for a project.

    Local Web IDE currently treats project types as optional. Return an empty list
    so the frontend can proceed without device-specific definitions.
    """
    return []
