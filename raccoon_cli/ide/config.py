"""Configuration for the IDE backend."""

from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class Settings:
    """IDE backend settings."""

    # Project root directory (where projects are stored)
    project_root: Path = field(default_factory=Path.cwd)

    # Template directory for project generation
    template_root: Path = field(default_factory=lambda: Path(__file__).parent.parent / "templates")

    # Simulation settings
    MISSION_SIMULATION_ENABLED: bool = False
    MISSION_SIMULATION_MIN_DELAY_MS: int = 50
    MISSION_SIMULATION_MAX_DELAY_MS: int = 200
    MISSION_SIMULATION_INCLUDE_STDOUT: bool = False
