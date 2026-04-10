"""Project management endpoints."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from raccoon.fingerprint import compute_fingerprint, default_exclude_patterns
from raccoon.server.auth import require_auth
from raccoon.sync_state import SyncState as PersistedSyncState
from raccoon.sync_state import read_sync_state, write_sync_state

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _load_exclude_patterns(project_path: Path) -> list[str]:
    """Build the fingerprint exclude list (defaults + .raccoonignore)."""
    patterns = default_exclude_patterns()
    ignore_file = project_path / ".raccoonignore"
    if ignore_file.exists():
        try:
            for line in ignore_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                patterns.append(line.rstrip("/\\"))
        except OSError:
            pass
    return patterns


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


# ---------------------------------------------------------------------------
# Fingerprint + sync state
# ---------------------------------------------------------------------------


class FingerprintResponse(BaseModel):
    """Project tree fingerprint."""

    project_id: str
    root_hash: str
    file_count: int
    total_bytes: int


class FingerprintFilesResponse(BaseModel):
    """Per-file hashes for diffing when fingerprints mismatch."""

    project_id: str
    root_hash: str
    files: dict[str, str]


class SyncState(BaseModel):
    """Persisted sync state for a project."""

    version: int
    fingerprint: Optional[str] = None
    synced_at: Optional[str] = None
    synced_by: Optional[str] = None


class UpdateSyncStateRequest(BaseModel):
    """Client request to bump the sync counter after a verified sync."""

    fingerprint: str = Field(..., min_length=64, max_length=64)
    expected_prev_version: int = Field(..., ge=0)
    synced_by: Optional[str] = None


def _get_project_path_or_404(project_id: str) -> Path:
    from raccoon.server.app import get_config
    from raccoon.server.services.project_manager import ProjectManager

    manager = ProjectManager(get_config().projects_dir)
    project_path = manager.get_project_path(project_id)
    if project_path is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project_path


@router.get("/{project_id}/fingerprint", response_model=FingerprintResponse)
async def get_project_fingerprint(project_id: str):
    """Compute and return the project's current content fingerprint.

    This walks the tree every call — intentional, because stale fingerprints
    would defeat the purpose. For typical Botball projects the cost is sub-second.
    """
    project_path = _get_project_path_or_404(project_id)
    patterns = _load_exclude_patterns(project_path)
    result = compute_fingerprint(project_path, exclude_patterns=patterns)
    return FingerprintResponse(
        project_id=project_id,
        root_hash=result.root_hash,
        file_count=result.file_count,
        total_bytes=result.total_bytes,
    )


@router.get("/{project_id}/fingerprint/files", response_model=FingerprintFilesResponse)
async def get_project_fingerprint_files(project_id: str):
    """Return per-file hashes so clients can diff on mismatch."""
    project_path = _get_project_path_or_404(project_id)
    patterns = _load_exclude_patterns(project_path)
    result = compute_fingerprint(project_path, exclude_patterns=patterns)
    return FingerprintFilesResponse(
        project_id=project_id,
        root_hash=result.root_hash,
        files=result.files,
    )


@router.get("/{project_id}/sync_state", response_model=SyncState)
async def get_project_sync_state(project_id: str):
    """Return the last persisted sync state (version + fingerprint snapshot)."""
    project_path = _get_project_path_or_404(project_id)
    state = read_sync_state(project_path)
    return SyncState(**state.to_dict())


@router.post(
    "/{project_id}/sync_state",
    response_model=SyncState,
    dependencies=[Depends(require_auth)],
)
async def update_project_sync_state(project_id: str, request: UpdateSyncStateRequest):
    """Bump the sync counter after a verified sync.

    The server is authoritative: it refuses the update if the client's
    ``expected_prev_version`` doesn't match what's on disk. That surfaces races
    between multiple clients as an explicit error the user can act on.

    The server also recomputes the fingerprint locally and requires it to match
    the one the client just verified, so nothing can fabricate an "in sync"
    state without the bytes actually matching.
    """
    project_path = _get_project_path_or_404(project_id)

    current = read_sync_state(project_path)
    if current.version != request.expected_prev_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Sync state version mismatch: server is at v{current.version}, "
                f"client expected v{request.expected_prev_version}. Re-sync to continue."
            ),
        )

    patterns = _load_exclude_patterns(project_path)
    actual = compute_fingerprint(project_path, exclude_patterns=patterns)
    if actual.root_hash != request.fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Server fingerprint does not match client's verified fingerprint. "
                "Files on the Pi changed between sync and state update; re-sync to continue."
            ),
        )

    new_state = PersistedSyncState(
        version=current.version + 1,
        fingerprint=request.fingerprint,
        synced_at=datetime.now(timezone.utc).isoformat(),
        synced_by=request.synced_by,
    )
    write_sync_state(project_path, new_state)
    return SyncState(**new_state.to_dict())
