"""Type definition API routes."""

from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends

from raccoon.ide.services.project_service import ProjectService

router = APIRouter()


def get_project_service() -> ProjectService:
    """Dependency injection for ProjectService - will be overridden by app."""
    raise NotImplementedError("ProjectService dependency not configured")


@router.get("/{project_uuid}", response_model=List[Dict[str, Any]])
async def get_type_definitions(
    project_uuid: UUID,
    svc: ProjectService = Depends(get_project_service),
) -> List[Dict[str, Any]]:
    """Return type definitions for a project.

    Reads definitions from the project's raccoon.project.yml and returns them
    as a list of objects with name, type, and any additional properties.
    """
    config = svc.project_repository.read_project_config(project_uuid)
    if not config:
        return []

    definitions = config.get("definitions", {})
    if not definitions or not isinstance(definitions, dict):
        return []

    result = []
    for name, props in definitions.items():
        if not isinstance(props, dict):
            continue
        # Create entry with name and all properties from the definition
        entry = {"name": name, **props}
        result.append(entry)

    return result
