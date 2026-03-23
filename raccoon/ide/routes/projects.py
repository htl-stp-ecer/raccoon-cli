"""Project API routes."""

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from raccoon.ide.schemas.project import Project, ProjectCreate
from raccoon.ide.services.project_service import ProjectService

router = APIRouter()


def get_project_service() -> ProjectService:
    """Dependency injection for ProjectService - will be overridden by app."""
    raise NotImplementedError("ProjectService dependency not configured")


@router.post("/", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(
        project_create: ProjectCreate,
        svc: ProjectService = Depends(get_project_service),
):
    """Create a scaffolded project in the IDE backend."""
    try:
        return svc.create_project(project_create)
    except FileExistsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{project_uuid}", response_model=Project)
async def get_project(
        project_uuid: UUID,
        svc: ProjectService = Depends(get_project_service),
):
    """Return one project by UUID."""
    project = svc.get_project(project_uuid)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


@router.put("/{project_uuid}", response_model=Project)
async def update_project(
        project_uuid: UUID,
        project_update: ProjectCreate,
        svc: ProjectService = Depends(get_project_service),
):
    """Rename or otherwise update a project record."""
    project = svc.update_project(project_uuid, project_update)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


@router.delete("/{project_uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
        project_uuid: UUID,
        svc: ProjectService = Depends(get_project_service),
):
    """Delete a project record and its backing directory."""
    if not svc.delete_project(project_uuid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )


@router.get("/", response_model=List[Project])
async def list_projects(
        svc: ProjectService = Depends(get_project_service),
):
    """List all projects known to the IDE backend."""
    return svc.list_projects()
