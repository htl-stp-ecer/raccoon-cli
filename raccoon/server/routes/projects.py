"""Project management endpoints."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon.server.auth import require_auth

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class ProjectInfo(BaseModel):
    """Project information model."""

    id: str
    name: str
    path: str
    has_config: bool
    last_modified: Optional[str] = None


class ProjectListResponse(BaseModel):
    """Response model for project list."""

    projects: list[ProjectInfo]
    count: int


@router.get("", response_model=ProjectListResponse)
async def list_projects():
    """
    List all projects on the Pi.

    Scans the projects directory for valid Raccoon projects
    (directories containing raccoon.project.yml).
    """
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager

    config = get_config()
    manager = ProjectManager(config.projects_dir)

    projects = manager.list_projects()

    return ProjectListResponse(
        projects=[
            ProjectInfo(
                id=p["id"],
                name=p["name"],
                path=str(p["path"]),
                has_config=p["has_config"],
                last_modified=p.get("last_modified"),
            )
            for p in projects
        ],
        count=len(projects),
    )


@router.get("/{project_id}", response_model=ProjectInfo)
async def get_project(project_id: str):
    """
    Get details for a specific project.
    """
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager

    config = get_config()
    manager = ProjectManager(config.projects_dir)

    project = manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    return ProjectInfo(
        id=project["id"],
        name=project["name"],
        path=str(project["path"]),
        has_config=project["has_config"],
        last_modified=project.get("last_modified"),
    )


@router.delete("/{project_id}", dependencies=[Depends(require_auth)])
async def delete_project(project_id: str):
    """
    Delete a project from the Pi.

    This permanently removes the project directory.
    """
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager

    config = get_config()
    manager = ProjectManager(config.projects_dir)

    success = manager.delete_project(project_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    return {"status": "deleted", "project_id": project_id}
