"""Mission API routes."""

from typing import List, Dict, Any
from uuid import UUID
import logging
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Body, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, field_validator

from raccoon.ide.core.project_code_gen import ProjectCodeGen
from raccoon.ide.core.naming import normalize_name
from raccoon.ide.schemas.mission import DiscoveredMission
from raccoon.ide.schemas.mission_detail import ParsedMission
from raccoon.ide.schemas.simulation import MissionSimulationData, ProjectSimulationData
from raccoon.ide.services.mission_service import MissionService

logger = logging.getLogger(__name__)


class CreateMissionRequest(BaseModel):
    """Payload for creating a new mission source file and config entry."""

    name: str = Field(..., min_length=1, max_length=200, description="Mission name")

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('Mission name cannot be empty or whitespace only')
        return v.strip()


class UpdateMissionOrderRequest(BaseModel):
    """Payload for reordering a mission within project configuration."""

    mission_name: str = Field(..., min_length=1, description="Mission name")
    order: int = Field(..., ge=0, description="New order position (0-based)")

    @field_validator('mission_name')
    @classmethod
    def validate_mission_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('Mission name cannot be empty or whitespace only')
        return v.strip()


class RenameMissionRequest(BaseModel):
    """Payload for renaming a mission class, file, and config references."""

    old_name: str = Field(..., min_length=1, description="Existing mission name")
    new_name: str = Field(..., min_length=1, max_length=200, description="New mission name")

    @field_validator('old_name', 'new_name')
    @classmethod
    def validate_names(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('Mission name cannot be empty or whitespace only')
        return v.strip()


router = APIRouter()


def get_mission_service() -> MissionService:
    """Dependency injection for MissionService - will be overridden by app."""
    raise NotImplementedError("MissionService dependency not configured")


def get_project_codegen() -> ProjectCodeGen:
    """Dependency injection for ProjectCodeGen - will be overridden by app."""
    raise NotImplementedError("ProjectCodeGen dependency not configured")


@router.get("/{project_uuid}", response_model=List[DiscoveredMission])
async def get_project_missions(
        project_uuid: UUID,
        svc: MissionService = Depends(get_mission_service),
):
    """List missions declared for a project."""
    try:
        missions = await asyncio.to_thread(svc.get_project_missions, project_uuid)
        return missions
    except Exception as e:
        logger.error(f"Failed to get missions for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while fetching missions")


@router.get("/{project_uuid}/detailed/{mission_name}", response_model=ParsedMission)
async def parse_mission_detailed(
        project_uuid: UUID,
        mission_name: str,
        svc: MissionService = Depends(get_mission_service),
):
    """Return the fully parsed mission document used by the visual editor."""
    if not mission_name or not mission_name.strip():
        raise HTTPException(status_code=400, detail="Mission name cannot be empty")

    mission_name = mission_name.strip()

    try:
        mission = await asyncio.to_thread(svc.get_detailed_mission_by_name, project_uuid, mission_name)
        if not mission:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_name}' not found or could not be parsed")
        return mission
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get detailed mission '{mission_name}' for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while fetching mission details")


@router.get("/{project_uuid}/source/{mission_name}")
async def get_mission_source(
        project_uuid: UUID,
        mission_name: str,
        svc: MissionService = Depends(get_mission_service),
):
    """Return the raw Python source code for a mission file."""
    if not mission_name or not mission_name.strip():
        raise HTTPException(status_code=400, detail="Mission name cannot be empty")

    mission_name = mission_name.strip()

    try:
        source = await asyncio.to_thread(svc.get_mission_source, project_uuid, mission_name)
        if source is None:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_name}' source not found")
        return {"name": mission_name, "source": source}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get source for mission '{mission_name}' in project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while fetching mission source")


class SaveMissionSourceRequest(BaseModel):
    """Payload for saving raw mission source code."""

    source: str = Field(..., description="Python source code")


@router.put("/{project_uuid}/source/{mission_name}")
async def save_mission_source(
        project_uuid: UUID,
        mission_name: str,
        request: SaveMissionSourceRequest,
        svc: MissionService = Depends(get_mission_service),
):
    """Save raw Python source code back to a mission file."""
    if not mission_name or not mission_name.strip():
        raise HTTPException(status_code=400, detail="Mission name cannot be empty")

    mission_name = mission_name.strip()

    try:
        success = await asyncio.to_thread(svc.save_mission_source, project_uuid, mission_name, request.source)
        if not success:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_name}' not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save source for mission '{mission_name}' in project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while saving mission source")


@router.post("/{project_uuid}")
async def create_mission(
        project_uuid: UUID,
        request: CreateMissionRequest,
        project_codegen: ProjectCodeGen = Depends(get_project_codegen),
):
    """Create a new mission in the project."""
    try:
        normalized_name = normalize_name(request.name)
        project_codegen.add_mission_to_project(project_uuid, normalized_name)
        logger.info(f"Successfully created mission '{normalized_name.pascal}' for project {project_uuid}")
        return {"success": True, "message": f"Mission '{normalized_name.pascal}' created successfully"}
    except FileExistsError as e:
        logger.warning(f"Mission '{request.name}' already exists in project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=409, detail=f"Mission '{request.name}' already exists in this project")
    except ValueError as e:
        logger.warning(f"Invalid mission name '{request.name}' for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid mission name: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"Project not found {project_uuid}: {str(e)}")
        raise HTTPException(status_code=404, detail="Project not found")
    except PermissionError as e:
        logger.error(f"Permission error creating mission for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        logger.error(f"Failed to create mission '{request.name}' for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while creating mission")


@router.put("/{project_uuid}/update")
async def update_mission_from_json(
        project_uuid: UUID,
        mission_data: Dict[str, Any] = Body(...),
        svc: MissionService = Depends(get_mission_service),
):
    """Update a mission file from JSON data."""
    if not mission_data:
        raise HTTPException(status_code=400, detail="Mission data cannot be empty")

    mission_name = mission_data.get('name', 'unknown')

    try:
        success = svc.update_mission_from_json(project_uuid, mission_data)
        if not success:
            logger.warning(f"Failed to update mission '{mission_name}' for project {project_uuid}")
            raise HTTPException(status_code=400, detail="Failed to update mission from JSON data")

        logger.info(f"Successfully updated mission '{mission_name}' for project {project_uuid}")
        return {"success": True, "message": f"Mission '{mission_name}' updated successfully"}
    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.error(f"Project not found {project_uuid}: {str(e)}")
        raise HTTPException(status_code=404, detail="Project not found")
    except PermissionError as e:
        logger.error(f"Permission error updating mission for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        logger.error(f"Failed to update mission '{mission_name}' for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while updating mission")


@router.put("/{project_uuid}/order")
async def update_mission_order(
        project_uuid: UUID,
        request: UpdateMissionOrderRequest,
        svc: MissionService = Depends(get_mission_service),
):
    """Update the order of a specific mission."""
    try:
        success = svc.update_mission_order(project_uuid, request.mission_name, request.order)
        if not success:
            logger.warning(f"Failed to update order for mission '{request.mission_name}' in project {project_uuid}")
            raise HTTPException(status_code=400, detail="Failed to update mission order")

        logger.info(f"Successfully updated order for mission '{request.mission_name}' to {request.order} in project {project_uuid}")
        return {"success": True, "message": f"Mission '{request.mission_name}' order updated to {request.order}"}
    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.error(f"Project or mission not found {project_uuid}: {str(e)}")
        raise HTTPException(status_code=404, detail="Project or mission not found")
    except PermissionError as e:
        logger.error(f"Permission error updating mission order for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        logger.error(f"Failed to update order for mission '{request.mission_name}' in project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while updating mission order")


@router.delete("/{project_uuid}/mission/{mission_name}")
async def delete_mission(
        project_uuid: UUID,
        mission_name: str,
        svc: MissionService = Depends(get_mission_service),
):
    """Delete a mission and remove its project references."""
    try:
        if not mission_name or not mission_name.strip():
            raise HTTPException(status_code=400, detail="Mission name cannot be empty")

        deleted = svc.delete_mission(project_uuid, mission_name)
        if not deleted:
            raise HTTPException(status_code=404, detail="Mission not found or could not be deleted")
        logger.info(f"Deleted mission '{mission_name}' for project {project_uuid}")
        return {"success": True, "message": f"Mission '{mission_name}' deleted"}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        logger.error(f"Failed to delete mission '{mission_name}' for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while deleting mission")


@router.put("/{project_uuid}/rename")
async def rename_mission(
        project_uuid: UUID,
        request: RenameMissionRequest,
        svc: MissionService = Depends(get_mission_service),
):
    """Rename a mission across source files, snapshots, and config."""
    try:
        success = svc.rename_mission(project_uuid, request.old_name, request.new_name)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to rename mission")
        logger.info(f"Renamed mission '{request.old_name}' to '{request.new_name}' for project {project_uuid}")
        return {"success": True, "message": f"Renamed mission '{request.old_name}' to '{request.new_name}'"}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        logger.error(f"Failed to rename mission '{request.old_name}' for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while renaming mission")


@router.websocket("/{project_uuid}/run/{mission_name}")
async def run_mission_ws(
        websocket: WebSocket,
        project_uuid: UUID,
        mission_name: str,
):
    """WebSocket that streams mission run output and step updates.

    Sends JSON messages:
      - {type: 'started', pid}
      - {type: 'stdout'|'stderr', line}
      - {type: 'step', index, timeline_index, name?, path?, parent_index?}
      - {type: 'planned_steps', steps}
      - {type: 'exit', returncode}
      - {type: 'error', message}
    """
    # Get service from app state
    svc: MissionService = websocket.app.state.mission_service

    await websocket.accept()
    try:
        # Optionally send the planned steps first (if available) to help clients
        try:
            detailed = svc.get_detailed_mission_by_name(project_uuid, mission_name)
            if detailed:
                steps_payload = svc.build_step_timeline(detailed)
                if steps_payload:
                    public_steps = [{k: v for k, v in step.items() if not str(k).startswith("_")} for step in steps_payload]
                    await websocket.send_json({"type": "planned_steps", "steps": public_steps})
        except Exception:
            # Non-fatal if analysis fails
            pass

        # Optional per-request simulation toggle via query param
        qp = websocket.query_params
        sim_param = qp.get("simulate") if qp is not None else None
        simulate = None
        if sim_param is not None:
            simulate = str(sim_param).lower() in {"1", "true", "yes", "on"}

        debug_param = qp.get("debug") if qp is not None else None
        debug_mode = None
        if debug_param is not None:
            debug_mode = str(debug_param).lower() in {"1", "true", "yes", "on"}

        async def producer():
            async for event in svc.stream_mission_output(project_uuid, mission_name, simulate=simulate, debug=debug_mode):
                await websocket.send_json(event)

        async def consumer():
            while True:
                try:
                    message = await websocket.receive_json()
                except asyncio.CancelledError:
                    break
                except WebSocketDisconnect:
                    raise
                except ValueError:
                    continue
                except Exception:
                    continue
                if not isinstance(message, dict):
                    continue
                msg_type = str(message.get("type") or "").lower()
                action = str(message.get("action") or "").lower()
                if msg_type in {"debug", "breakpoint"} and action in {"resume", "continue", "resume_breakpoint"}:
                    svc.resume_breakpoint(project_uuid)

        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        tasks = {producer_task, consumer_task}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
    except WebSocketDisconnect:
        # Client disconnected; try to stop the mission gracefully
        try:
            await svc.stop_mission(project_uuid)
        except Exception:
            pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        logger.error(f"WebSocket error while running mission '{mission_name}' for project {project_uuid}: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/{project_uuid}/stop")
async def stop_mission(
        project_uuid: UUID,
        svc: MissionService = Depends(get_mission_service),
):
    """Stop the currently running mission for the project, if any."""
    try:
        result = await svc.stop_mission(project_uuid)
        return result
    except Exception as e:
        logger.error(f"Failed to stop mission for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while stopping mission")


@router.get("/{project_uuid}/simulation", response_model=ProjectSimulationData)
async def get_all_missions_simulation(
        project_uuid: UUID,
        svc: MissionService = Depends(get_mission_service),
):
    """Get simulation data for all missions in a project."""
    try:
        missions_data = await asyncio.to_thread(svc.get_all_missions_simulation_data, project_uuid)
        return ProjectSimulationData(missions=missions_data)
    except Exception as e:
        logger.error(f"Failed to get simulation data for project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while fetching simulation data")


@router.get("/{project_uuid}/simulation/{mission_name}", response_model=MissionSimulationData)
async def get_mission_simulation(
        project_uuid: UUID,
        mission_name: str,
        svc: MissionService = Depends(get_mission_service),
):
    """Get simulation data for a specific mission."""
    if not mission_name or not mission_name.strip():
        raise HTTPException(status_code=400, detail="Mission name cannot be empty")

    mission_name = mission_name.strip()

    try:
        sim_data = await asyncio.to_thread(svc.get_mission_simulation_data, project_uuid, mission_name)
        if not sim_data:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_name}' not found or could not be analyzed")
        return sim_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get simulation data for mission '{mission_name}' in project {project_uuid}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while fetching mission simulation data")
