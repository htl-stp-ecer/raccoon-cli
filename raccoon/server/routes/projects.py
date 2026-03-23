"""Project management endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

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


class CreateProjectRequest(BaseModel):
    """Create-project request payload."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_\-\s]+$",
    )


def _serialize_project(project: dict) -> ProjectInfo:
    return ProjectInfo(
        id=project["id"],
        name=project["name"],
        path=str(project["path"]),
        has_config=project["has_config"],
        last_modified=project.get("last_modified"),
    )


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
        projects=[_serialize_project(project) for project in projects],
        count=len(projects),
    )


@router.post("", response_model=ProjectInfo, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_auth)])
async def create_project(request: CreateProjectRequest):
    """
    Create a new project on the Pi.

    Uses `raccoon create project` in non-interactive mode.
    """
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager

    config = get_config()
    manager = ProjectManager(config.projects_dir)

    try:
        project = manager.create_project(request.name)
    except FileExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    return _serialize_project(project)


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

    return _serialize_project(project)


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
