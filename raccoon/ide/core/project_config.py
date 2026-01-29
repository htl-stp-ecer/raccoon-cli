from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

SPECIAL_MISSION_TYPES = {"setup", "shutdown"}


def ensure_mission_list(config: Dict[str, Any]) -> List[Any]:
    missions = config.get("missions")
    if isinstance(missions, list):
        return missions
    missions = []
    config["missions"] = missions
    return missions


def mission_entry_name(entry: Any) -> Optional[str]:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and entry:
        key = next(iter(entry.keys()))
        return str(key)
    return None


def mission_entry_kind(entry: Any) -> Optional[str]:
    if isinstance(entry, dict) and entry:
        value = next(iter(entry.values()))
        if isinstance(value, str):
            return value.lower()
        if isinstance(value, dict):
            candidate = value.get("kind") or value.get("type") or value.get("role")
            if candidate is not None:
                return str(candidate).lower()
    return None


def is_special_mission(entry: Any) -> bool:
    kind = mission_entry_kind(entry)
    return bool(kind and kind in SPECIAL_MISSION_TYPES)


def replace_mission_name(entry: Any, new_name: str) -> Any:
    if isinstance(entry, str):
        return new_name
    if isinstance(entry, dict) and entry:
        key = next(iter(entry.keys()))
        value = entry[key]
        return {new_name: deepcopy(value)}
    return entry


def append_mission_if_missing(config: Dict[str, Any], mission_name: str) -> bool:
    missions = ensure_mission_list(config)
    for entry in missions:
        if mission_entry_name(entry) == mission_name:
            return False
    missions.append(mission_name)
    return True


def remove_mission_entry(config: Dict[str, Any], mission_name: str) -> bool:
    missions = ensure_mission_list(config)
    for idx, entry in enumerate(missions):
        if mission_entry_name(entry) == mission_name:
            missions.pop(idx)
            return True
    return False


def rename_mission_entry(config: Dict[str, Any], old_name: str, new_name: str) -> bool:
    missions = ensure_mission_list(config)
    for idx, entry in enumerate(missions):
        if mission_entry_name(entry) == old_name:
            missions[idx] = replace_mission_name(entry, new_name)
            return True
    return False
