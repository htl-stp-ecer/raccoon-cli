"""Schemas for step simulation data used by the frontend to compute and display robot paths."""

from typing import List, Optional
from pydantic import BaseModel


class SimulationDelta(BaseModel):
    """Position/orientation change produced by a step."""
    forward: float = 0.0  # displacement in meters (positive = forward)
    strafe: float = 0.0   # displacement in meters (positive = right)
    angular: float = 0.0  # rotation in radians (positive = counter-clockwise)


class SimulationStepData(BaseModel):
    """Simulation data for a single step."""
    path: List[int]                    # hierarchical path e.g. [1, 2, 3]
    function_name: str
    step_type: str
    label: Optional[str] = None        # display label
    average_duration_ms: float = 100.0
    duration_stddev_ms: float = 10.0
    delta: SimulationDelta
    children: Optional[List['SimulationStepData']] = None


class MissionSimulationData(BaseModel):
    """Complete simulation data for a mission including all steps."""
    name: str
    is_setup: bool = False
    is_shutdown: bool = False
    order: int = 0
    steps: List[SimulationStepData]
    total_duration_ms: float = 0.0
    total_delta: SimulationDelta      # aggregated position change


class ProjectSimulationData(BaseModel):
    """Simulation data for all missions in a project."""
    missions: List[MissionSimulationData]


# Pydantic v2 rebuild for self-referential types
SimulationStepData.model_rebuild()
