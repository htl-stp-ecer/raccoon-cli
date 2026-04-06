"""Helpers for reading and mutating the ``missions`` section of project config."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

SPECIAL_MISSION_TYPES = {"setup", "shutdown"}


def ensure_mission_list(config: Dict[str, Any]) -> List[Any]:
    """Return the mutable mission list, creating an empty one when missing."""
    missions = config.get("missions")
    if isinstance(missions, list):
        return missions
    missions = []
    config["missions"] = missions
    return missions


def mission_entry_name(entry: Any) -> Optional[str]:
    """Extract a mission name from a string or mapping-style mission entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and entry:
        key = next(iter(entry.keys()))
        return str(key)
    return None


def mission_entry_kind(entry: Any) -> Optional[str]:
    """Extract the logical mission kind such as ``setup`` or ``shutdown``."""
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
    """Return ``True`` when the entry represents a setup or shutdown mission."""
    kind = mission_entry_kind(entry)
    return bool(kind and kind in SPECIAL_MISSION_TYPES)


def replace_mission_name(entry: Any, new_name: str) -> Any:
    """Return a copy of ``entry`` with its mission name replaced."""
    if isinstance(entry, str):
        return new_name
    if isinstance(entry, dict) and entry:
        key = next(iter(entry.keys()))
        value = entry[key]
        return {new_name: deepcopy(value)}
    return entry


def append_mission_if_missing(config: Dict[str, Any], mission_name: str) -> bool:
    """Append ``mission_name`` unless an entry with that name already exists."""
    missions = ensure_mission_list(config)
    for entry in missions:
        if mission_entry_name(entry) == mission_name:
            return False
    missions.append(mission_name)
    return True


def remove_mission_entry(config: Dict[str, Any], mission_name: str) -> bool:
    """Remove the named mission entry if present."""
    missions = ensure_mission_list(config)
    for idx, entry in enumerate(missions):
        if mission_entry_name(entry) == mission_name:
            missions.pop(idx)
            return True
    return False


def rename_mission_entry(config: Dict[str, Any], old_name: str, new_name: str) -> bool:
    """Rename a mission entry in-place when it exists."""
    missions = ensure_mission_list(config)
    for idx, entry in enumerate(missions):
        if mission_entry_name(entry) == old_name:
            missions[idx] = replace_mission_name(entry, new_name)
            return True
    return False


# ---------------------------------------------------------------------------
# Convenience functions that mutate config AND persist via save_project_keys
# ---------------------------------------------------------------------------


def add_mission_to_config(project_root: Path, mission_class: str) -> bool:
    """Add a mission to the project config and persist via save_project_keys."""
    from raccoon.project import load_project_config, save_project_keys

    config = load_project_config(project_root)
    if append_mission_if_missing(config, mission_class):
        save_project_keys(project_root, {"missions": config["missions"]})
        return True
    return False


def remove_mission_from_config(project_root: Path, mission_class: str) -> bool:
    """Remove a mission from the project config and persist via save_project_keys."""
    from raccoon.project import load_project_config, save_project_keys

    config = load_project_config(project_root)
    if remove_mission_entry(config, mission_class):
        save_project_keys(project_root, {"missions": config["missions"]})
        return True
    return False
