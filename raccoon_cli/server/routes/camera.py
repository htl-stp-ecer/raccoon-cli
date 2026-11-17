"""Camera backends: which there are (built-in and in projects), what they can be configured with, the choice.

The logic is in ``raccoon_cli.camera_discovery``; this only turns it into HTTP. The chosen backend is kept by
raccoon-cam, whose service applies it on its own, so nothing has to be sent to the service from here.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from raccoon_cli import camera_discovery
from raccoon_cli.server.auth import require_auth

router = APIRouter(prefix="/api/v1/camera", tags=["camera"])


class BackendListResponse(BaseModel):
    backends: list[dict[str, Any]]
    count: int


class ConfigBody(BaseModel):
    path: str = Field(description='"module:Class" of the backend')
    cfg: dict[str, Any] = Field(default_factory=dict)
    project_id: Optional[str] = Field(default=None, description="The project the class is in, none for a built-in one")


def _manager():
    from raccoon_cli.server.app import get_config
    from raccoon_cli.server.services.project_manager import ProjectManager

    return ProjectManager(get_config().projects_dir)


def _project_dir(project_id: Optional[str]):
    if project_id is None:
        return None
    path = _manager().get_project_path(project_id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project not found: {project_id}")
    return path


def _failure(error: Exception) -> HTTPException:
    if isinstance(error, camera_discovery.CameraUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(error))


@router.get("/backends", response_model=BackendListResponse)
def list_backends():
    """Built-in backends, then the backend classes found in the projects on this robot."""
    try:
        backends = camera_discovery.list_backends(_manager().list_projects())
    except (camera_discovery.CameraUnavailable, camera_discovery.CameraDiscoveryError) as error:
        raise _failure(error)
    return BackendListResponse(backends=backends, count=len(backends))


@router.get("/backends/describe")
def describe_backend(path: str, project_id: Optional[str] = None):
    """One backend with its options (name, type, default, range, choices, description)."""
    project_dir = _project_dir(project_id)
    try:
        description = camera_discovery.describe_class(path, project_dir)
    except (camera_discovery.CameraUnavailable, camera_discovery.CameraDiscoveryError) as error:
        raise _failure(error)
    project = _manager().get_project(project_id) if project_id else None
    return {**description, "origin": camera_discovery.origin(project), "project_id": project_id}


@router.get("/config")
def get_choice():
    """The backend chosen on this robot, or ``null`` if none was."""
    try:
        return {"backend": camera_discovery.get_choice()}
    except camera_discovery.CameraUnavailable as error:
        raise _failure(error)


@router.put("/config", dependencies=[Depends(require_auth)])
def put_choice(body: ConfigBody):
    """Check the configuration against the backend's options and keep it as the chosen backend."""
    project_dir = _project_dir(body.project_id)
    try:
        saved = camera_discovery.save_choice(body.path, body.cfg, project_dir)
    except (camera_discovery.CameraUnavailable, camera_discovery.CameraDiscoveryError) as error:
        raise _failure(error)
    return {"saved": True, "backend": saved}


@router.delete("/config", dependencies=[Depends(require_auth)])
def delete_choice():
    """Forget the chosen backend: the camera service goes back to what its unit configures."""
    try:
        camera_discovery.clear_choice()
    except camera_discovery.CameraUnavailable as error:
        raise _failure(error)
    return {"cleared": True}
