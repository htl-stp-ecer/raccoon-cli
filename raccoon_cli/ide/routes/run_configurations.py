"""Run-configuration CRUD endpoints for the Web-IDE.

These wrap the shared :mod:`raccoon_cli.run_configurations` module so the
IDE and the ``raccoon run`` CLI stay in lock-step — same storage, same
schema, same builtin presets. The IDE just gets a thin REST surface on
top.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from raccoon_cli.ide.repositories.project_repository import ProjectRepository
from raccoon_cli.project import ProjectError
from raccoon_cli.run_configurations import (
    RunConfiguration,
    delete_run_configuration,
    load_run_configurations,
    upsert_run_configuration,
)


logger = logging.getLogger(__name__)


router = APIRouter()


def get_project_repository() -> ProjectRepository:
    """Dependency injection hook overridden in ``app.py``."""
    raise NotImplementedError("ProjectRepository dependency not configured")


class RunConfigurationPayload(BaseModel):
    """Wire format for a run configuration. Mirrors the dataclass."""

    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    target: str = "auto"
    dev: bool = False
    no_calibrate: bool = False
    no_checkpoints: bool = False
    no_codegen: bool = False
    no_sync: bool = False
    record_localization: bool = False
    record_hz: Optional[float] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    builtin: bool = False


def _serialize(cfg: RunConfiguration) -> Dict[str, Any]:
    return {
        "name": cfg.name,
        "description": cfg.description,
        "target": cfg.target,
        "dev": cfg.dev,
        "no_calibrate": cfg.no_calibrate,
        "no_checkpoints": cfg.no_checkpoints,
        "no_codegen": cfg.no_codegen,
        "no_sync": cfg.no_sync,
        "record_localization": cfg.record_localization,
        "record_hz": cfg.record_hz,
        "args": list(cfg.args),
        "env": dict(cfg.env),
        "builtin": cfg.builtin,
    }


def _resolve_project_root(
    project_uuid: UUID,
    repo: ProjectRepository,
):
    project_path = repo.get_project_path(project_uuid)
    if not project_path or not project_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    return project_path


@router.get("/{project_uuid}")
async def list_run_configurations(
    project_uuid: UUID,
    repo: ProjectRepository = Depends(get_project_repository),
):
    """Return every run configuration (builtin + user-defined) for a project."""
    project_path = _resolve_project_root(project_uuid, repo)
    try:
        configs = load_run_configurations(project_path)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Sort: builtins first (in canonical order), then alphabetical user entries.
    builtin_order = ["default", "dev", "simulated"]
    items = []
    for name in builtin_order:
        if name in configs:
            items.append(_serialize(configs[name]))
    seen = set(builtin_order)
    for name in sorted(configs.keys()):
        if name in seen:
            continue
        items.append(_serialize(configs[name]))
    return {"configurations": items}


@router.put("/{project_uuid}/{name}")
async def upsert_run_configuration_endpoint(
    project_uuid: UUID,
    name: str,
    payload: RunConfigurationPayload,
    repo: ProjectRepository = Depends(get_project_repository),
):
    """Create or replace a run configuration by *name*."""
    project_path = _resolve_project_root(project_uuid, repo)
    if payload.name != name:
        raise HTTPException(
            status_code=400,
            detail="Path name and payload name must match",
        )
    cfg = RunConfiguration(
        name=payload.name,
        description=payload.description,
        target=payload.target,
        dev=payload.dev,
        no_calibrate=payload.no_calibrate,
        no_checkpoints=payload.no_checkpoints,
        no_codegen=payload.no_codegen,
        no_sync=payload.no_sync,
        record_localization=payload.record_localization,
        record_hz=payload.record_hz,
        args=list(payload.args),
        env=dict(payload.env),
        builtin=False,
    )
    try:
        upsert_run_configuration(project_path, cfg)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "name": cfg.name}


@router.delete("/{project_uuid}/{name}")
async def delete_run_configuration_endpoint(
    project_uuid: UUID,
    name: str,
    repo: ProjectRepository = Depends(get_project_repository),
):
    """Remove a user-defined configuration. Builtins return 400."""
    project_path = _resolve_project_root(project_uuid, repo)
    try:
        delete_run_configuration(project_path, name)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}
