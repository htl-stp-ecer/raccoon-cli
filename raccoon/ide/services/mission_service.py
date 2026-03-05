"""Mission management service used by the IDE API layer.

The service bridges persisted project configuration, source-code analysis, JSON
mission editing, breakpoint-driven execution, and simulation data generation.
"""

from __future__ import annotations

from typing import List, Optional, AsyncIterator, Dict, Any
from uuid import UUID
import logging
import json
from pathlib import Path

import libcst as cst
from contextlib import suppress

from raccoon.ide.core.analysis.detailed_mission_analyzer import DetailedMissionAnalyzer
from raccoon.ide.core.analysis.mission_analyzer import MissionAnalyzer
from raccoon.ide.core.mission_code_generator import MissionUpdater
from raccoon.commands.remove_cmd import _remove_mission_import_from_main
from raccoon.ide.core.project_config import (
    ensure_mission_list,
    is_special_mission,
    mission_entry_name,
    remove_mission_entry,
    rename_mission_entry,
)
from raccoon.ide.core.naming import normalize_name
from raccoon.ide.repositories.project_repository import ProjectRepository
from raccoon.ide.schemas.mission import DiscoveredMission
from raccoon.ide.schemas.mission_detail import ParsedMission, ParsedStep, Vector2D, ParsedComment, ParsedGroup, StepArgument
from raccoon.ide.schemas.simulation import SimulationDelta, SimulationStepData, MissionSimulationData
from raccoon.ide.config import Settings
import asyncio
from asyncio.subprocess import PIPE
import re
import random
import time
from typing import Optional as _OptionalBool

logger = logging.getLogger(__name__)


class MissionService:
    """Coordinate mission discovery, editing, execution, and simulation."""

    _STEP_LAYOUT_FILENAME = "mission_layouts.json"
    _MISSION_SNAPSHOT_DIRNAME = ".mission_snapshots"

    def __init__(self, project_repository: ProjectRepository, settings: Settings | None = None):
        self._repo = project_repository
        self._settings = settings or Settings()
        self._detailed_analyzer = DetailedMissionAnalyzer()
        self._mission_updater = MissionUpdater()
        self._mission_analyzer = MissionAnalyzer()
        # Track running mission processes per project
        self._running_procs: dict[UUID, asyncio.subprocess.Process] = {}
        # Simulation cancel flags per project
        self._sim_cancel: dict[UUID, asyncio.Event] = {}
        # Breakpoint wait handles per project (simulation/debug mode only)
        self._breakpoint_waiters: dict[UUID, asyncio.Event] = {}

    @staticmethod
    def build_step_timeline(mission: ParsedMission | None) -> List[Dict[str, Any]]:
        """Flatten mission steps into a sequential list carrying only index information."""
        flattened: List[Dict[str, Any]] = []
        if not mission or not getattr(mission, "steps", None):
            return flattened

        def token_variants(raw: str) -> List[str]:
            raw = (raw or "").strip()
            if not raw:
                return []
            tokens: List[str] = []

            def add(candidate: str | None) -> None:
                candidate = (candidate or "").strip()
                if candidate and candidate not in tokens:
                    tokens.append(candidate)

            add(raw)
            underscored = raw.replace("_", " ")
            add(underscored)
            add(raw.replace("_", ""))
            add(underscored.replace(" ", ""))
            normalized = raw.replace("-", "_")
            camel = "".join(segment.capitalize() for segment in normalized.split("_") if segment)
            add(camel)
            parts = [segment for segment in normalized.split("_") if segment]
            for part in parts:
                add(part)
                add(part.capitalize())
            return tokens

        def visit(
            step_list,
            *,
            path_prefix: tuple[int, ...] = (),
            parent_index: int | None = None,
        ):
            for ordinal, step in enumerate(step_list or [], start=1):
                index = len(flattened) + 1
                path_tuple = (*path_prefix, ordinal)
                payload: Dict[str, Any] = {
                    "index": index,
                    "path": list(path_tuple),
                }
                if parent_index is not None:
                    payload["parent_index"] = parent_index

                match_tokens: List[str] = []
                path_label = ".".join(str(p) for p in path_tuple)
                if path_label:
                    match_tokens.append(path_label)

                function_name = getattr(step, "function_name", None)
                if function_name:
                    payload["function_name"] = function_name
                    match_tokens.extend(token_variants(function_name))

                step_type = getattr(step, "step_type", None)
                if step_type:
                    payload["step_type"] = step_type
                    if not function_name or step_type != function_name:
                        match_tokens.extend(token_variants(step_type))

                arguments = getattr(step, "arguments", None) or []
                arguments_payload: List[Dict[str, Any]] = []
                if arguments:
                    arg_labels: List[str] = []
                    for argument in arguments:
                        value = getattr(argument, "value", None)
                        if isinstance(value, str):
                            value_repr = value
                        else:
                            value_repr = str(value)
                        resolved_type = StepArgument.binding_for(argument)
                        arg_name = getattr(argument, "name", None)
                        if resolved_type == "keyword" and arg_name:
                            label = f"{arg_name}={value_repr}"
                        else:
                            label = value_repr
                        if label:
                            arg_labels.append(label)
                            match_tokens.append(label)
                        arguments_payload.append({
                            "name": arg_name,
                            "value": value,
                            "type": resolved_type,
                        })
                    if function_name and arg_labels:
                        display_label = f"{function_name}({', '.join(arg_labels)})"
                        payload["display_label"] = display_label
                        match_tokens.append(display_label)
                if arguments_payload:
                    payload["arguments"] = arguments_payload
                    payload["args"] = arguments_payload

                if match_tokens:
                    payload["_match_tokens"] = list(dict.fromkeys(tok for tok in match_tokens if tok))

                name_value = payload.get("display_label") or function_name or step_type
                if name_value:
                    payload["name"] = name_value

                flattened.append(payload)
                children = getattr(step, "children", None) or []
                if children:
                    visit(
                        children,
                        path_prefix=path_tuple,
                        parent_index=index,
                    )

        visit(list(getattr(mission, "steps", []) or []))
        return flattened

    @staticmethod
    def _after_last_normal_index(missions: List[Any]) -> int:
        for idx in range(len(missions) - 1, -1, -1):
            if not is_special_mission(missions[idx]):
                return idx + 1
        return 0

    def _positions_file_path(self, project_path: Path) -> Path:
        return project_path / self._STEP_LAYOUT_FILENAME

    def _collect_step_positions(self, steps_payload: Any, path_prefix: tuple[int, ...] = ()) -> Dict[str, Dict[str, float]]:
        positions: Dict[str, Dict[str, float]] = {}
        if not isinstance(steps_payload, list):
            return positions

        for ordinal, step_payload in enumerate(steps_payload, start=1):
            if not isinstance(step_payload, dict):
                continue
            path = (*path_prefix, ordinal)
            pos_payload = step_payload.get("position")
            if isinstance(pos_payload, dict):
                x = pos_payload.get("x")
                y = pos_payload.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    key = ".".join(str(segment) for segment in path)
                    positions[key] = {"x": float(x), "y": float(y)}

            child_payload = step_payload.get("children") or []
            if child_payload:
                positions.update(self._collect_step_positions(child_payload, path))

        return positions

    def _collect_comment_data(self, comments_payload: Any) -> Dict[str, Dict[str, Any]]:
        comment_data: Dict[str, Dict[str, Any]] = {}
        if not isinstance(comments_payload, list):
            return comment_data

        for entry in comments_payload:
            if not isinstance(entry, dict):
                continue
            comment_id = entry.get("id")
            if not isinstance(comment_id, str) or not comment_id:
                continue

            comment_payload: Dict[str, Any] = {}
            raw_text = entry.get("text")
            if raw_text is None:
                comment_payload["text"] = ""
            else:
                comment_payload["text"] = raw_text if isinstance(raw_text, str) else str(raw_text)

            position_payload = entry.get("position")
            if isinstance(position_payload, dict):
                x = position_payload.get("x")
                y = position_payload.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    comment_payload["position"] = {"x": float(x), "y": float(y)}

            before_path = entry.get("before_path")
            if isinstance(before_path, str):
                comment_payload["before_path"] = before_path

            after_path = entry.get("after_path")
            if isinstance(after_path, str):
                comment_payload["after_path"] = after_path

            comment_data[comment_id] = comment_payload

        return comment_data

    def _collect_group_data(self, groups_payload: Any) -> Dict[str, Dict[str, Any]]:
        group_data: Dict[str, Dict[str, Any]] = {}
        if not isinstance(groups_payload, list):
            return group_data

        for entry in groups_payload:
            if not isinstance(entry, dict):
                continue
            group_id = entry.get("id")
            if not isinstance(group_id, str) or not group_id:
                continue

            group_payload: Dict[str, Any] = {}

            title = entry.get("title")
            if isinstance(title, str) and title.strip():
                group_payload["title"] = title

            position_payload = entry.get("position")
            if isinstance(position_payload, dict):
                x = position_payload.get("x")
                y = position_payload.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    group_payload["position"] = {"x": float(x), "y": float(y)}

            size_payload = entry.get("size")
            if isinstance(size_payload, dict):
                w = size_payload.get("width")
                h = size_payload.get("height")
                if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                    group_payload["size"] = {"width": float(w), "height": float(h)}

            expanded_size_payload = entry.get("expanded_size")
            if isinstance(expanded_size_payload, dict):
                w = expanded_size_payload.get("width")
                h = expanded_size_payload.get("height")
                if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                    group_payload["expanded_size"] = {"width": float(w), "height": float(h)}

            collapsed = entry.get("collapsed")
            if isinstance(collapsed, bool):
                group_payload["collapsed"] = collapsed

            step_paths = entry.get("step_paths")
            if isinstance(step_paths, list):
                normalized: List[str] = []
                for p in step_paths:
                    if isinstance(p, str) and p and p not in normalized:
                        normalized.append(p)
                if normalized:
                    group_payload["step_paths"] = normalized

            group_data[group_id] = group_payload

        return group_data

    def _persist_step_positions(self, project_path: Path, mission_name: str, mission_data: Dict[str, Any]) -> None:
        if not mission_name:
            return
        try:
            file_path = self._positions_file_path(project_path)
            positions = self._collect_step_positions(mission_data.get("steps") or [])
            comments_data = self._collect_comment_data(mission_data.get("comments") or [])
            groups_data = self._collect_group_data(mission_data.get("groups") or [])

            payload: Dict[str, Any] = {"missions": {}}
            if file_path.exists():
                try:
                    payload = json.loads(file_path.read_text(encoding="utf-8")) or {"missions": {}}
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Failed to decode mission layout file %s: %s", file_path, exc
                    )
                    payload = {"missions": {}}

            missions_section = payload.setdefault("missions", {})
            mission_entry = missions_section.setdefault(mission_name, {})
            mission_entry["positions"] = positions
            if comments_data:
                mission_entry["comments"] = comments_data
            else:
                mission_entry.pop("comments", None)
            if groups_data:
                mission_entry["groups"] = groups_data
            else:
                mission_entry.pop("groups", None)

            file_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Failed to persist step positions for mission '%s' in project %s: %s",
                mission_name,
                project_path,
                exc,
            )

    def _load_comments(self, project_path: Path, mission_name: str) -> List[ParsedComment]:
        file_path = self._positions_file_path(project_path)
        if not file_path.exists():
            return []
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load mission comments for '%s' in project %s: %s",
                mission_name,
                project_path,
                exc,
            )
            return []

        missions_section = payload.get("missions")
        if not isinstance(missions_section, dict):
            return []

        mission_entry = missions_section.get(mission_name)
        if not isinstance(mission_entry, dict):
            return []

        raw_comments = mission_entry.get("comments")
        if not isinstance(raw_comments, dict):
            return []

        comments: List[ParsedComment] = []
        for comment_id, entry in raw_comments.items():
            if not isinstance(comment_id, str):
                continue
            details = entry if isinstance(entry, dict) else {}
            raw_text = details.get("text")
            text_value = raw_text if isinstance(raw_text, str) else (str(raw_text) if raw_text is not None else "")

            before_path = details.get("before_path")
            after_path = details.get("after_path")

            position_payload = details.get("position")
            position_dict = None
            if isinstance(position_payload, dict):
                x = position_payload.get("x")
                y = position_payload.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    position_dict = {"x": float(x), "y": float(y)}

            comments.append(
                ParsedComment(
                    id=comment_id,
                    text=text_value,
                    before_path=before_path if isinstance(before_path, str) else None,
                    after_path=after_path if isinstance(after_path, str) else None,
                    position=position_dict,
                )
            )

        return comments

    def _load_groups(self, project_path: Path, mission_name: str) -> List[ParsedGroup]:
        file_path = self._positions_file_path(project_path)
        if not file_path.exists():
            return []
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load mission groups for '%s' in project %s: %s",
                mission_name,
                project_path,
                exc,
            )
            return []

        missions_section = payload.get("missions")
        if not isinstance(missions_section, dict):
            return []

        mission_entry = missions_section.get(mission_name)
        if not isinstance(mission_entry, dict):
            return []

        raw_groups = mission_entry.get("groups")
        if not isinstance(raw_groups, dict):
            return []

        groups: List[ParsedGroup] = []
        for group_id, entry in raw_groups.items():
            if not isinstance(group_id, str):
                continue
            details = entry if isinstance(entry, dict) else {}

            title = details.get("title")
            title_value = title if isinstance(title, str) and title.strip() else "Group"

            position_payload = details.get("position")
            position_dict = None
            if isinstance(position_payload, dict):
                x = position_payload.get("x")
                y = position_payload.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    position_dict = {"x": float(x), "y": float(y)}

            size_payload = details.get("size")
            size_dict = None
            if isinstance(size_payload, dict):
                w = size_payload.get("width")
                h = size_payload.get("height")
                if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                    size_dict = {"width": float(w), "height": float(h)}

            expanded_size_payload = details.get("expanded_size")
            expanded_size_dict = None
            if isinstance(expanded_size_payload, dict):
                w = expanded_size_payload.get("width")
                h = expanded_size_payload.get("height")
                if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                    expanded_size_dict = {"width": float(w), "height": float(h)}

            collapsed = details.get("collapsed")
            collapsed_value = collapsed if isinstance(collapsed, bool) else False

            step_paths_payload = details.get("step_paths")
            step_paths: List[str] = []
            if isinstance(step_paths_payload, list):
                for p in step_paths_payload:
                    if isinstance(p, str) and p and p not in step_paths:
                        step_paths.append(p)

            groups.append(
                ParsedGroup(
                    id=group_id,
                    title=title_value,
                    position=position_dict,
                    size=size_dict,
                    expanded_size=expanded_size_dict,
                    collapsed=collapsed_value,
                    step_paths=step_paths,
                )
            )

        return groups

    def _apply_persisted_comments(self, mission: ParsedMission, comments: List[ParsedComment]) -> None:
        mission.comments = comments

    def _apply_persisted_groups(self, mission: ParsedMission, groups: List[ParsedGroup]) -> None:
        mission.groups = groups

    def _load_step_positions(self, project_path: Path, mission_name: str) -> Dict[str, Dict[str, float]]:
        file_path = self._positions_file_path(project_path)
        if not file_path.exists():
            return {}
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load mission layout file %s: %s",
                file_path,
                exc,
            )
            return {}

        missions_section = payload.get("missions")
        if not isinstance(missions_section, dict):
            return {}

        mission_entry = missions_section.get(mission_name)
        if not isinstance(mission_entry, dict):
            return {}

        positions = mission_entry.get("positions")
        if not isinstance(positions, dict):
            return {}

        filtered: Dict[str, Dict[str, float]] = {}
        for key, value in positions.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            x = value.get("x")
            y = value.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                filtered[key] = {"x": float(x), "y": float(y)}
        return filtered

    def _apply_persisted_positions(self, mission: ParsedMission, positions: Dict[str, Dict[str, float]]) -> None:
        if not positions or not mission.steps:
            return

        def assign(steps: List[Any], path_prefix: tuple[int, ...] = ()) -> None:
            for ordinal, step in enumerate(steps or [], start=1):
                path = (*path_prefix, ordinal)
                key = ".".join(str(segment) for segment in path)
                pos_payload = positions.get(key)
                if isinstance(pos_payload, dict):
                    x = pos_payload.get("x")
                    y = pos_payload.get("y")
                    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                        try:
                            step.position = Vector2D(x=x, y=y)
                        except Exception:
                            logger.debug(
                                "Unable to assign persisted position %s to step %s", pos_payload, key
                            )
                children = getattr(step, "children", None) or []
                if children:
                    assign(children, path)

        assign(list(mission.steps))

    def _mission_snapshot_dir(self, project_path: Path) -> Path:
        return project_path / self._MISSION_SNAPSHOT_DIRNAME

    def _mission_snapshot_path(self, project_path: Path, mission_name: str) -> Path:
        normalized = normalize_name(mission_name or "mission").snake
        filename = f"{normalized}.json"
        return self._mission_snapshot_dir(project_path) / filename

    def _persist_mission_snapshot(self, project_path: Path, mission_data: dict) -> None:
        try:
            mission_name = str(mission_data.get("name") or "").strip()
            if not mission_name:
                return
            snapshot_dir = self._mission_snapshot_dir(project_path)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = self._mission_snapshot_path(project_path, mission_name)
            snapshot_path.write_text(
                json.dumps(mission_data, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Unable to persist mission snapshot for %s: %s", project_path, exc)

    def _load_mission_snapshot(self, project_path: Path, mission_name: str) -> Dict[str, Any] | None:
        snapshot_path = self._mission_snapshot_path(project_path, mission_name)
        if not snapshot_path.exists():
            return None
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Unable to load mission snapshot %s: %s", snapshot_path, exc)
            return None

    def _mission_file_path(self, project_path: Path, mission_name: str) -> Path | None:
        missions_dir = project_path / "src" / "missions"
        if not missions_dir.exists():
            return None
        normalized = normalize_name(mission_name).snake
        for candidate in sorted(missions_dir.glob(f"{normalized}*.py")):
            if candidate.is_file():
                return candidate
        target_class = f"class {mission_name}"
        for candidate in missions_dir.glob("*.py"):
            try:
                content = candidate.read_text(encoding="utf-8")
            except Exception:
                continue
            if target_class in content:
                return candidate
        return None

    def _count_breakpoints(self, mission: ParsedMission | None) -> int:
        if not mission or not mission.steps:
            return 0

        def _count(steps: List[Any]) -> int:
            total = 0
            for step in steps or []:
                step_type = getattr(step, "step_type", "") or ""
                function_name = getattr(step, "function_name", "") or ""
                if isinstance(step_type, str) and step_type.lower() == "breakpoint":
                    total += 1
                elif isinstance(function_name, str) and function_name.lower() == "breakpoint":
                    total += 1
                children = getattr(step, "children", None) or []
                if children:
                    total += _count(children)
            return total

        return _count(list(mission.steps or []))

    def _should_prefer_snapshot(
        self,
        project_path: Path,
        mission_name: str,
        detailed: ParsedMission,
        snapshot: ParsedMission,
    ) -> bool:
        snapshot_breakpoints = self._count_breakpoints(snapshot)
        if snapshot_breakpoints <= 0:
            return False
        detailed_breakpoints = self._count_breakpoints(detailed)
        if snapshot_breakpoints <= detailed_breakpoints:
            return False
        mission_path = self._mission_file_path(project_path, mission_name)
        snapshot_path = self._mission_snapshot_path(project_path, mission_name)
        if mission_path and snapshot_path.exists():
            try:
                return snapshot_path.stat().st_mtime >= mission_path.stat().st_mtime
            except OSError:
                return True
        return True

    def _apply_basic_flags(
        self,
        mission: ParsedMission,
        basics: List[DiscoveredMission],
        mission_name: str,
    ) -> None:
        for basic in basics:
            if basic.name == mission_name:
                mission.is_setup = basic.is_setup
                mission.is_shutdown = basic.is_shutdown
                mission.order = basic.order
                return


    def get_project_missions(self, project_uuid: UUID) -> List[DiscoveredMission]:
        try:
            project_path = self._repo.get_project_path(project_uuid)
            if not project_path or not project_path.exists():
                logger.warning(f"Project directory does not exist for UUID: {project_uuid}")
                return []

            config = self._repo.read_project_config(project_uuid)
            missions = self._mission_analyzer.discover_from_config(config)
            if not missions:
                logger.debug(f"No missions declared in raccoon.project.yml for project {project_uuid}")
            return missions
        except Exception as e:
            logger.error(f"Unexpected error getting missions for project {project_uuid}: {e}")
            return []

    def get_detailed_mission_by_name(self, project_uuid: UUID, mission_name: str) -> Optional[ParsedMission]:
        """Get detailed mission information including steps and arguments."""
        if not mission_name or not mission_name.strip():
            logger.warning(f"Empty mission name provided for project {project_uuid}")
            return None

        mission_name = mission_name.strip()

        try:
            project_path = self._repo.get_project_path(project_uuid)
            if not project_path:
                logger.warning(f"Project path not found for UUID: {project_uuid}")
                return None

            if not project_path.exists():
                logger.warning(f"Project directory does not exist: {project_path}")
                return None

            persisted_positions = self._load_step_positions(project_path, mission_name)
            persisted_comments = self._load_comments(project_path, mission_name)
            persisted_groups = self._load_groups(project_path, mission_name)
            basic_missions = self.get_project_missions(project_uuid)

            snapshot_data = self._load_mission_snapshot(project_path, mission_name)
            snapshot_mission: ParsedMission | None = None
            if snapshot_data:
                try:
                    validator = getattr(ParsedMission, "model_validate", None)
                    if callable(validator):
                        snapshot_mission = validator(snapshot_data)
                    else:
                        snapshot_mission = ParsedMission.parse_obj(snapshot_data)
                except Exception as exc:
                    logger.debug(
                        "Unable to validate mission snapshot for '%s' in project %s: %s",
                        mission_name,
                        project_uuid,
                        exc,
                    )
                    snapshot_mission = None

            detailed_mission = self._detailed_analyzer.analyze_mission_by_name(project_path, mission_name)

            def apply_enrichments(target: ParsedMission | None) -> None:
                if not target:
                    return
                if persisted_positions:
                    self._apply_persisted_positions(target, persisted_positions)
                if persisted_comments:
                    self._apply_persisted_comments(target, persisted_comments)
                if persisted_groups:
                    self._apply_persisted_groups(target, persisted_groups)
                self._apply_basic_flags(target, basic_missions, mission_name)

            apply_enrichments(detailed_mission)
            apply_enrichments(snapshot_mission)

            chosen: ParsedMission | None = detailed_mission
            if snapshot_mission and (
                chosen is None
                or self._should_prefer_snapshot(project_path, mission_name, chosen, snapshot_mission)
            ):
                chosen = snapshot_mission
                if detailed_mission:
                    logger.debug(
                        "Using mission snapshot for '%s' in project %s because analyzer output lacks breakpoint metadata",
                        mission_name,
                        project_uuid,
                    )

            if chosen is None:
                logger.debug(
                    "Mission '%s' not found or could not be analyzed for project %s",
                    mission_name,
                    project_uuid,
                )

            return chosen

        except Exception as e:
            logger.error(f"Error getting detailed mission '{mission_name}' for project {project_uuid}: {e}")
            return None

    def update_mission_from_json(self, project_uuid: UUID, mission_data: dict) -> bool:
        """Update a mission file from JSON data."""
        if not mission_data:
            logger.warning(f"Empty mission data provided for project {project_uuid}")
            return False

        try:
            project_path = self._repo.get_project_path(project_uuid)
            if not project_path:
                logger.warning(f"Project path not found for UUID: {project_uuid}")
                return False

            if not project_path.exists():
                logger.warning(f"Project directory does not exist: {project_path}")
                return False

            mission_name = mission_data.get('name', 'unknown')
            logger.debug(f"Updating mission '{mission_name}' for project {project_uuid}")

            result = self._mission_updater.update_mission_from_json(project_path, mission_data)

            if result:
                logger.debug(f"Successfully updated mission '{mission_name}' for project {project_uuid}")
                self._persist_step_positions(project_path, mission_name, mission_data)
                self._persist_mission_snapshot(project_path, mission_data)
            else:
                logger.warning(f"Mission updater returned False for mission '{mission_name}' in project {project_uuid}")

            return result

        except Exception as e:
            mission_name = mission_data.get('name', 'unknown') if mission_data else 'unknown'
            logger.error(f"Error updating mission '{mission_name}' for project {project_uuid}: {e}")
            return False

    def update_mission_order(self, project_uuid: UUID, mission_name: str, new_order: int) -> bool:
        """Reorder a mission inside raccoon.project.yml. Setup/shutdown missions are fixed."""
        try:
            if not mission_name or not mission_name.strip():
                logger.warning(f"Empty mission name provided for project {project_uuid}")
                return False

            mission_name = mission_name.strip()

            project_path = self._repo.get_project_path(project_uuid)
            if not project_path:
                logger.warning(f"Project path not found for UUID: {project_uuid}")
                return False

            if not project_path.exists():
                logger.warning(f"Project directory does not exist for UUID: {project_uuid}")
                return False

            config = self._repo.read_project_config(project_uuid)
            missions_payload = ensure_mission_list(config)
            target_idx = next(
                (idx for idx, entry in enumerate(missions_payload) if mission_entry_name(entry) == mission_name),
                None,
            )
            if target_idx is None:
                logger.warning(f"Mission '{mission_name}' not found in project {project_uuid}")
                return False
            if is_special_mission(missions_payload[target_idx]):
                logger.warning(f"Cannot reorder setup/shutdown mission '{mission_name}' for project {project_uuid}")
                return False

            normal_indices = [idx for idx, entry in enumerate(missions_payload) if not is_special_mission(entry)]
            current_pos = normal_indices.index(target_idx)
            desired_pos = max(0, min(int(new_order), len(normal_indices) - 1))
            if desired_pos == current_pos:
                logger.debug(
                    "Mission '%s' already at requested order %s in project %s",
                    mission_name,
                    new_order,
                    project_uuid,
                )
                return False

            entry = missions_payload.pop(target_idx)
            remaining_normals = [idx for idx, entry in enumerate(missions_payload) if not is_special_mission(entry)]
            if desired_pos >= len(remaining_normals):
                insert_idx = self._after_last_normal_index(missions_payload)
            else:
                insert_idx = remaining_normals[desired_pos]
            missions_payload.insert(insert_idx, entry)
            self._repo.write_project_config(project_uuid, config)
            return True
        except Exception as e:
            logger.error(
                f"Error updating order for mission '{mission_name}' in project {project_uuid}: {e}"
            )
            return False

    def delete_mission(self, project_uuid: UUID, mission_name: str) -> bool:
        """Delete a mission: remove file, unregister from raccoon.project.yml, and clean robot.py."""
        try:
            if not mission_name or not mission_name.strip():
                logger.warning(f"Empty mission name provided for deletion in project {project_uuid}")
                return False

            nn = normalize_name(mission_name)
            mission_class = f"{nn.pascal}Mission"
            mission_module = f"{nn.snake}_mission"
            project_path = self._repo.get_project_path(project_uuid)
            if not project_path or not project_path.exists():
                logger.warning(f"Project path not found/deleted for UUID: {project_uuid}")
                return False

            config = self._repo.read_project_config(project_uuid)
            refs_removed = remove_mission_entry(config, mission_class)
            if refs_removed:
                self._repo.write_project_config(project_uuid, config)

            # Remove import from main.py
            _remove_mission_import_from_main(project_path, nn.snake, nn.pascal)

            # Delete the mission file if present
            missions_dir = project_path / "src" / "missions"
            deleted = False
            if missions_dir.exists():
                exact = missions_dir / f"{mission_module}.py"
                if exact.exists():
                    exact.unlink()
                    deleted = True
                else:
                    # Fallback: delete first file that starts with snake
                    for p in missions_dir.glob(f"{nn.snake}*.py"):
                        p.unlink()
                        deleted = True
                        break

            any_changes = deleted or refs_removed
            if any_changes:
                snapshot_path = self._mission_snapshot_path(project_path, mission_name)
                if snapshot_path.exists():
                    try:
                        snapshot_path.unlink()
                    except OSError as exc:
                        logger.debug(
                            "Unable to remove mission snapshot %s for project %s: %s",
                            snapshot_path,
                            project_uuid,
                            exc,
                        )

            return any_changes
        except Exception as e:
            logger.error(f"Error deleting mission '{mission_name}' for project {project_uuid}: {e}")
            return False

    def rename_mission(self, project_uuid: UUID, old_name: str, new_name: str) -> bool:
        """Rename a mission class and file, and update references in raccoon.project.yml."""
        try:
            if not old_name or not old_name.strip() or not new_name or not new_name.strip():
                logger.warning(f"Invalid mission names provided for rename in project {project_uuid}")
                return False

            oldn = normalize_name(old_name)
            newn = normalize_name(new_name)
            old_class = f"{oldn.pascal}Mission"
            new_class = f"{newn.pascal}Mission"

            project_path = self._repo.get_project_path(project_uuid)
            if not project_path or not project_path.exists():
                logger.warning(f"Project path not found/deleted for UUID: {project_uuid}")
                return False

            config = self._repo.read_project_config(project_uuid)
            refs_changed = rename_mission_entry(config, old_class, new_class)
            if refs_changed:
                self._repo.write_project_config(project_uuid, config)

            # Rename the mission file and class definition
            missions_dir = project_path / "src" / "missions"
            changed = False
            if missions_dir.exists():
                # Find the file
                src_file = missions_dir / f"{oldn.snake}_mission.py"
                if not src_file.exists():
                    # fallback
                    for p in missions_dir.glob(f"{oldn.snake}*.py"):
                        src_file = p
                        break

                if src_file and src_file.exists():
                    try:
                        # Update class name inside the file (rename class def name)
                        code = src_file.read_text(encoding="utf-8")
                        module = cst.parse_module(code)

                        class _ClassRename(cst.CSTTransformer):
                            def __init__(self, old_cls: str, new_cls: str):
                                self.old = old_cls
                                self.new = new_cls
                                self.updated = False

                            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                                if isinstance(updated_node.name, cst.Name) and updated_node.name.value == self.old:
                                    self.updated = True
                                    return updated_node.with_changes(name=cst.Name(self.new))
                                return updated_node

                        xf = _ClassRename(old_class, new_class)
                        new_module = module.visit(xf)
                        if xf.updated and new_module.code != code:
                            src_file.write_text(new_module.code, encoding="utf-8")
                            changed = True
                    except Exception as e:
                        logger.warning(f"Failed to update mission class name in file {src_file}: {e}")

                    # Rename file if necessary
                    dst_file = missions_dir / f"{newn.snake}_mission.py"
                    if src_file.exists() and src_file.name != dst_file.name:
                        try:
                            src_file.rename(dst_file)
                            changed = True
                        except Exception as e:
                            logger.warning(f"Failed to rename mission file {src_file.name} -> {dst_file.name}: {e}")

            old_snapshot = self._mission_snapshot_path(project_path, old_name)
            new_snapshot = self._mission_snapshot_path(project_path, new_name)
            if old_snapshot.exists():
                try:
                    new_snapshot.parent.mkdir(parents=True, exist_ok=True)
                    old_snapshot.rename(new_snapshot)
                except OSError as exc:
                    logger.debug(
                        "Unable to rename mission snapshot %s -> %s for project %s: %s",
                        old_snapshot,
                        new_snapshot,
                        project_uuid,
                        exc,
                    )

            return changed or refs_changed
        except Exception as e:
            logger.error(f"Error renaming mission '{old_name}' to '{new_name}' for project {project_uuid}: {e}")
            return False

    def resume_breakpoint(self, project_uuid: UUID) -> bool:
        """Resume from a breakpoint in simulation/debug mode."""
        waiter = self._breakpoint_waiters.get(project_uuid)
        if waiter and not waiter.is_set():
            waiter.set()
            return True
        return False

    async def stop_mission(self, project_uuid: UUID) -> dict:
        """Stop a running mission for the given project."""
        # Stop simulation if running
        cancel_event = self._sim_cancel.get(project_uuid)
        if cancel_event:
            cancel_event.set()

        # Resume any blocked breakpoint so simulation exits cleanly
        self.resume_breakpoint(project_uuid)

        # Stop real process if running
        proc = self._running_procs.pop(project_uuid, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            return {"stopped": True, "pid": proc.pid}

        return {"stopped": False, "message": "No running mission found"}

    async def stream_mission_output(
        self,
        project_uuid: UUID,
        mission_name: str,
        *,
        simulate: _OptionalBool[bool] = None,
        debug: _OptionalBool[bool] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Run the mission and yield structured websocket events with live output.

        Events yielded (JSON-serializable dicts):
        - {type: 'started', pid: int}
        - {type: 'stdout', line: str}
        - {type: 'stderr', line: str}
        - {type: 'step', index: int, timeline_index: int, name?: str, path?: List[int], parent_index?: int}
        - {type: 'breakpoint', state: 'waiting'|'resumed'|'cancelled', ...metadata}
        - {type: 'exit', returncode: int}
        """
        try:
            def _stamped(event: Dict[str, Any]) -> Dict[str, Any]:
                """Return a shallow copy of the event with a unix timestamp."""
                return {**event, "timestamp": time.time()}

            if not mission_name or not mission_name.strip():
                logger.warning(f"Empty mission name provided for run in project {project_uuid}")
                return

            mission_name = mission_name.strip()

            project_path = self._repo.get_project_path(project_uuid)
            if not project_path or not project_path.exists():
                logger.warning(f"Project path not found/deleted for UUID: {project_uuid}")
                return

            # Validate mission exists
            available = {m.name for m in self.get_project_missions(project_uuid)}
            if mission_name not in available:
                logger.warning(
                    f"Requested mission '{mission_name}' not found in project {project_uuid}. Available: {sorted(available)}"
                )
                return

            # Check if we should simulate
            do_sim = self._settings.MISSION_SIMULATION_ENABLED if simulate is None else bool(simulate)
            debug_mode = bool(debug) if debug is not None else False

            if not do_sim:
                # Local execution via run.sh
                run_script = project_path / "run.sh"
                if not run_script.exists():
                    logger.warning(f"run.sh not found for project {project_uuid} at {run_script}")
                    yield _stamped({"type": "error", "message": "run.sh not found"})
                    return

            # Build planned step timeline for enrichment
            flattened_steps: List[Dict[str, Any]] = []
            try:
                detailed = self.get_detailed_mission_by_name(project_uuid, mission_name)
            except Exception:
                detailed = None
            if detailed:
                flattened_steps = self.build_step_timeline(detailed)

            if do_sim:
                # Simulation mode - yield fake events
                async for event in self._simulate_mission(project_uuid, mission_name, flattened_steps, debug_mode):
                    yield event
            else:
                # Real execution - run run.sh and stream output
                async for event in self._execute_mission(project_uuid, project_path, mission_name, flattened_steps):
                    yield event

        except Exception as e:
            logger.error(f"Error streaming mission '{mission_name}' for project {project_uuid}: {e}")
            yield {"type": "error", "message": str(e), "timestamp": time.time()}

    async def _simulate_mission(
        self,
        project_uuid: UUID,
        mission_name: str,
        flattened_steps: List[Dict[str, Any]],
        debug_mode: bool,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Simulate mission execution with fake delays."""
        def _stamped(event: Dict[str, Any]) -> Dict[str, Any]:
            return {**event, "timestamp": time.time()}

        cancel_event = asyncio.Event()
        self._sim_cancel[project_uuid] = cancel_event

        try:
            yield _stamped({"type": "started", "pid": -1})

            sim_timeline = flattened_steps or [
                {"index": i + 1, "path": [i + 1]}
                for i in range(3)
            ]

            min_ms = max(0, int(self._settings.MISSION_SIMULATION_MIN_DELAY_MS))
            max_ms = max(min_ms, int(self._settings.MISSION_SIMULATION_MAX_DELAY_MS))

            for i, step_data in enumerate(sim_timeline):
                if cancel_event.is_set():
                    break

                event = {
                    "type": "step",
                    "index": i + 1,
                    "timeline_index": step_data.get("index", i + 1),
                }
                for key in ("path", "parent_index", "function_name", "step_type", "display_label", "name"):
                    if key in step_data:
                        event[key] = step_data[key]

                yield _stamped(event)

                # Check for breakpoint
                is_breakpoint = any(
                    "breakpoint" in str(step_data.get(k, "")).lower()
                    for k in ("step_type", "function_name", "display_label", "name")
                )

                if debug_mode and is_breakpoint:
                    waiter = asyncio.Event()
                    self._breakpoint_waiters[project_uuid] = waiter
                    yield _stamped({"type": "breakpoint", "state": "waiting", "index": i + 1})

                    cancel_task = asyncio.create_task(cancel_event.wait())
                    resume_task = asyncio.create_task(waiter.wait())
                    try:
                        done, pending = await asyncio.wait(
                            {cancel_task, resume_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                        state = "resumed" if resume_task in done else "cancelled"
                    finally:
                        self._breakpoint_waiters.pop(project_uuid, None)

                    yield _stamped({"type": "breakpoint", "state": state, "index": i + 1})
                    if state != "resumed":
                        break

                delay = random.uniform(min_ms, max_ms) / 1000.0
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

            rc = 0 if not cancel_event.is_set() else 130
            yield _stamped({"type": "exit", "returncode": rc})

        finally:
            self._sim_cancel.pop(project_uuid, None)
            self._breakpoint_waiters.pop(project_uuid, None)

    async def _execute_mission(
        self,
        project_uuid: UUID,
        project_path: Path,
        mission_name: str,
        flattened_steps: List[Dict[str, Any]],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Execute the mission via run.sh and stream output."""
        def _stamped(event: Dict[str, Any]) -> Dict[str, Any]:
            return {**event, "timestamp": time.time()}

        proc = await asyncio.create_subprocess_exec(
            "bash", "run.sh", mission_name,
            cwd=str(project_path),
            stdout=PIPE,
            stderr=PIPE,
        )
        self._running_procs[project_uuid] = proc

        yield _stamped({"type": "started", "pid": proc.pid})

        async def read_stream(stream, stream_type):
            while True:
                line = await stream.readline()
                if not line:
                    break
                yield _stamped({"type": stream_type, "line": line.decode("utf-8", errors="replace").rstrip()})

        # Read stdout and stderr concurrently
        stdout_task = asyncio.create_task(self._collect_lines(proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(self._collect_lines(proc.stderr, "stderr"))

        # Yield lines as they come
        while not stdout_task.done() or not stderr_task.done():
            done, pending = await asyncio.wait(
                {stdout_task, stderr_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=0.1
            )
            # Check for any completed reads
            for task in done:
                for event in task.result():
                    yield _stamped(event)
                # Re-create the task if the stream isn't exhausted
                if task is stdout_task and proc.stdout:
                    stdout_task = asyncio.create_task(self._collect_lines(proc.stdout, "stdout"))
                elif task is stderr_task and proc.stderr:
                    stderr_task = asyncio.create_task(self._collect_lines(proc.stderr, "stderr"))

        await proc.wait()
        self._running_procs.pop(project_uuid, None)

        yield _stamped({"type": "exit", "returncode": proc.returncode})

    async def _collect_lines(self, stream, stream_type: str) -> List[Dict[str, Any]]:
        """Collect a batch of lines from a stream."""
        events = []
        if stream is None:
            return events
        try:
            line = await asyncio.wait_for(stream.readline(), timeout=0.1)
            if line:
                events.append({"type": stream_type, "line": line.decode("utf-8", errors="replace").rstrip()})
        except asyncio.TimeoutError:
            pass
        return events

    def get_mission_simulation_data(self, project_uuid: UUID, mission_name: str) -> Optional[MissionSimulationData]:
        """Get simulation data for a specific mission."""
        detailed = self.get_detailed_mission_by_name(project_uuid, mission_name)
        if not detailed:
            return None

        steps_data = self._build_simulation_steps(detailed.steps)
        total_duration = sum(s.average_duration_ms for s in steps_data)
        total_delta = self._aggregate_deltas([s.delta for s in steps_data])

        return MissionSimulationData(
            name=detailed.name,
            is_setup=detailed.is_setup,
            is_shutdown=detailed.is_shutdown,
            order=detailed.order,
            steps=steps_data,
            total_duration_ms=total_duration,
            total_delta=total_delta,
        )

    def get_all_missions_simulation_data(self, project_uuid: UUID) -> List[MissionSimulationData]:
        """Get simulation data for all missions in a project."""
        missions = self.get_project_missions(project_uuid)
        result = []
        for mission in missions:
            sim_data = self.get_mission_simulation_data(project_uuid, mission.name)
            if sim_data:
                result.append(sim_data)
        return result

    def _build_simulation_steps(self, steps: List[ParsedStep], path_prefix: List[int] = None) -> List[SimulationStepData]:
        """Build simulation step data from parsed steps."""
        if path_prefix is None:
            path_prefix = []

        result = []
        for i, step in enumerate(steps or [], start=1):
            path = path_prefix + [i]
            delta = self._estimate_step_delta(step)
            children = None
            if step.children:
                children = self._build_simulation_steps(step.children, path)

            result.append(SimulationStepData(
                path=path,
                function_name=step.function_name,
                step_type=step.step_type,
                label=step.function_name,
                average_duration_ms=100.0,
                duration_stddev_ms=10.0,
                delta=delta,
                children=children,
            ))
        return result

    def _estimate_step_delta(self, step: ParsedStep) -> SimulationDelta:
        """Estimate position change from a step (basic implementation)."""
        # This is a simplified estimation - real implementation would need
        # actual robot parameters and step-specific calculations
        func = step.function_name.lower()
        delta = SimulationDelta()

        for arg in step.arguments:
            if arg.name == "cm" and isinstance(arg.value, (int, float)):
                cm = float(arg.value)
                if "forward" in func:
                    delta.forward = cm / 100.0
                elif "backward" in func:
                    delta.forward = -cm / 100.0
                elif "strafe_left" in func:
                    delta.strafe = -cm / 100.0
                elif "strafe_right" in func:
                    delta.strafe = cm / 100.0
            elif arg.name == "deg" and isinstance(arg.value, (int, float)):
                import math
                deg = float(arg.value)
                if "cw" in func:
                    delta.angular = -math.radians(deg)
                elif "ccw" in func:
                    delta.angular = math.radians(deg)

        return delta

    def _aggregate_deltas(self, deltas: List[SimulationDelta]) -> SimulationDelta:
        """Aggregate multiple deltas into one (simplified)."""
        total = SimulationDelta()
        for d in deltas:
            total.forward += d.forward
            total.strafe += d.strafe
            total.angular += d.angular
        return total
