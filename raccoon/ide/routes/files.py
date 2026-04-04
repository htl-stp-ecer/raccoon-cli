"""File CRUD endpoints – lets the IDE editor read and write project source files."""

import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon.ide.services.project_service import ProjectService

router = APIRouter()

# Only expose these extensions in the file browser
_ALLOWED_EXTENSIONS = {
    ".py", ".yml", ".yaml", ".json", ".txt", ".md", ".toml", ".cfg", ".ini"
}

# Skip these directories entirely
_EXCLUDED_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    ".raccoon", ".raccoon_cache", "dist", "build",
}


def get_project_service() -> ProjectService:
    """Dependency – overridden by the application factory."""
    raise NotImplementedError("ProjectService dependency not configured")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_project_path(project_uuid: str, svc: ProjectService) -> Path:
    from uuid import UUID
    try:
        uid = UUID(project_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project UUID")

    project = svc.get_project(uid)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return svc.get_project_path(uid)


def _safe_child(project_path: Path, relative: str) -> Path:
    """Resolve *relative* inside *project_path*, raising 403 on traversal."""
    clean = relative.replace("\\", "/").lstrip("/")
    resolved = (project_path / clean).resolve()
    if not str(resolved).startswith(str(project_path.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    return resolved


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FileItem(BaseModel):
    path: str   # relative to project root, forward-slashes
    name: str


class FileContentResponse(BaseModel):
    path: str
    content: str


class FileContentUpdate(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{project_uuid}", response_model=List[FileItem])
async def list_files(
    project_uuid: str,
    svc: ProjectService = Depends(get_project_service),
):
    """Return a flat list of editable files inside the project directory."""
    project_path = _resolve_project_path(project_uuid, svc)

    items: List[FileItem] = []
    for root, dirs, files in os.walk(project_path):
        # Prune excluded / hidden directories in-place
        dirs[:] = sorted(
            d for d in dirs
            if d not in _EXCLUDED_DIRS and not d.startswith(".")
        )

        rel_root = Path(root).relative_to(project_path)
        for fname in sorted(files):
            if Path(fname).suffix.lower() in _ALLOWED_EXTENSIONS:
                rel = str(rel_root / fname) if str(rel_root) != "." else fname
                items.append(FileItem(path=rel.replace("\\", "/"), name=fname))

    return items


@router.get("/{project_uuid}/content", response_model=FileContentResponse)
async def get_file_content(
    project_uuid: str,
    path: str,
    svc: ProjectService = Depends(get_project_service),
):
    """Return the UTF-8 content of a project file."""
    project_path = _resolve_project_path(project_uuid, svc)
    file_path = _safe_child(project_path, path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}")

    return FileContentResponse(path=path, content=content)


@router.put("/{project_uuid}/content")
async def update_file_content(
    project_uuid: str,
    body: FileContentUpdate,
    svc: ProjectService = Depends(get_project_service),
):
    """Overwrite a project file with new UTF-8 content."""
    project_path = _resolve_project_path(project_uuid, svc)
    file_path = _safe_child(project_path, body.path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.write_text(body.content, encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {exc}")

    return {"success": True, "path": body.path}
