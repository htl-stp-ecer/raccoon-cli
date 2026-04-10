"""Read mission declarations from ``raccoon.project.yml`` configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

from raccoon_cli.ide.core.project_config import (
    ensure_mission_list,
    is_special_mission,
    mission_entry_name,
    mission_entry_kind,
)
from raccoon_cli.ide.schemas.mission import DiscoveredMission


class MissionAnalyzer:
    """
    Builds DiscoveredMission models from raccoon.project.yml configuration.
    """

    def discover_from_project_root(self, project_root: Path) -> List[DiscoveredMission]:
        config_path = project_root / "raccoon.project.yml"
        if not config_path.exists():
            return []

        try:
            from raccoon_cli.yaml_utils import load_yaml
            data = load_yaml(config_path)
        except Exception:
            return []

        if not isinstance(data, dict):
            data = {}
        return self.discover_from_config(data)

    def discover_from_config(self, config: Dict[str, Any]) -> List[DiscoveredMission]:
        missions_payload = ensure_mission_list(config)
        discoveries: List[DiscoveredMission] = []
        positional_order = 0

        for entry in missions_payload:
            name = mission_entry_name(entry)
            if not name:
                continue
            kind = mission_entry_kind(entry)
            is_setup = kind == "setup"
            is_shutdown = kind == "shutdown"
            order = -1 if is_special_mission(entry) else positional_order
            if not is_special_mission(entry):
                positional_order += 1
            discoveries.append(
                DiscoveredMission(
                    name=name,
                    is_setup=is_setup,
                    is_shutdown=is_shutdown,
                    order=order,
                )
            )
        return discoveries
