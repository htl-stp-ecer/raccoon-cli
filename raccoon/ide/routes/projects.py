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
    try:
        return svc.create_project(project_create)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{project_uuid}", response_model=Project)
async def get_project(
        project_uuid: UUID,
        svc: ProjectService = Depends(get_project_service),
):
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
    if not svc.delete_project(project_uuid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )


@router.get("/", response_model=List[Project])
async def list_projects(
        svc: ProjectService = Depends(get_project_service),
):
    return svc.list_projects()
