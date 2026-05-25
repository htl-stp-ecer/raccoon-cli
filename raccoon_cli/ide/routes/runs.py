"""Run / localization-replay endpoints.

The recorder in ``raccoon-lib`` writes
``<project_root>/.raccoon/runs/<UTC-ISO-timestamp>/localization.jsonl`` during a
mission run. These endpoints let the Web-IDE list those recordings, pull a
single recording back (streamed), inspect metadata, and delete it.

All filesystem logic lives in :class:`RunRepository`; this module is a
thin HTTP adapter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterator, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from raccoon_cli.ide.repositories.run_repository import RunRepository, RunSummary

logger = logging.getLogger(__name__)

router = APIRouter()


def get_run_repository() -> RunRepository:
    """Dependency placeholder -- overridden by the application factory."""
    raise NotImplementedError("RunRepository dependency not configured")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class RunSummaryResponse(BaseModel):
    """JSON shape returned for run summaries.

    Mirrors :class:`RunSummary`; we use a Pydantic model so OpenAPI docs
    stay accurate.
    """

    run_id: str
    started_at: str
    has_localization: bool
    file_size_bytes: int
    frame_count: int | None = None
    duration_ms: int | None = None

    @classmethod
    def from_summary(cls, summary: RunSummary) -> "RunSummaryResponse":
        return cls(**summary.to_dict())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{project_uuid}", response_model=List[RunSummaryResponse])
async def list_runs(
    project_uuid: str,
    repo: RunRepository = Depends(get_run_repository),
):
    """List recorded runs (only those with a localization.jsonl)."""
    try:
        summaries = await asyncio.to_thread(repo.list_runs, project_uuid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    return [RunSummaryResponse.from_summary(s) for s in summaries]


@router.get("/{project_uuid}/{run_id}/metadata", response_model=RunSummaryResponse)
async def get_run_metadata(
    project_uuid: str,
    run_id: str,
    repo: RunRepository = Depends(get_run_repository),
):
    """Return metadata for a single run, including lazily-computed fields."""
    try:
        summary = await asyncio.to_thread(repo.get_run_metadata, project_uuid, run_id)
    except FileNotFoundError as exc:
        msg = str(exc)
        if msg.startswith("recording_missing"):
            raise HTTPException(status_code=404, detail="recording_missing")
        if msg.startswith("Project not found"):
            raise HTTPException(status_code=404, detail="Project not found")
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunSummaryResponse.from_summary(summary)


@router.get("/{project_uuid}/{run_id}/localization")
async def stream_localization(
    project_uuid: str,
    run_id: str,
    repo: RunRepository = Depends(get_run_repository),
):
    """Stream the raw ``localization.jsonl`` for a run.

    Content-Encoding is left to the HTTP layer / reverse proxy so gzip
    happens transparently. We do set a download-friendly disposition so
    browser caching of the streaming response is well-behaved.
    """
    try:
        file_path = await asyncio.to_thread(
            repo.get_localization_path, project_uuid, run_id
        )
    except FileNotFoundError as exc:
        msg = str(exc)
        if msg.startswith("recording_missing"):
            raise HTTPException(status_code=404, detail="recording_missing")
        if msg.startswith("Project not found"):
            raise HTTPException(status_code=404, detail="Project not found")
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError:
        raise HTTPException(status_code=404, detail="Run not found")

    def _iter_file() -> Iterator[bytes]:
        # 64 KB chunks: big enough to amortize syscalls, small enough to
        # not stall an event loop iteration.
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                yield chunk

    headers = {
        "Content-Disposition": (
            f'attachment; filename="run-{run_id}-localization.jsonl"'
        ),
    }
    return StreamingResponse(
        _iter_file(),
        media_type="application/x-ndjson",
        headers=headers,
    )


@router.delete("/{project_uuid}/{run_id}", status_code=204)
async def delete_run(
    project_uuid: str,
    run_id: str,
    repo: RunRepository = Depends(get_run_repository),
):
    """Delete a single recorded run."""
    try:
        await asyncio.to_thread(repo.delete_run, project_uuid, run_id)
    except FileNotFoundError as exc:
        msg = str(exc)
        if msg.startswith("Project not found"):
            raise HTTPException(status_code=404, detail="Project not found")
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run id")
    return None
