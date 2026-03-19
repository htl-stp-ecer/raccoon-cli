"""Discover available DSL steps for the IDE and maintain a libstp cache."""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from uuid import UUID

from raccoon.ide.core.analysis.step_analyzer import DSLStepAnalyzer, StepFunction, StepArgument
from raccoon.ide.services.project_service import ProjectService


class StepDiscoveryService:
    """Collect step definitions from cached library data and project source."""

    # How long discovered file-based steps stay cached (seconds).
    _DISCOVERY_TTL = 10.0

    def __init__(self, project_service: ProjectService):
        self.project_service = project_service
        self._libstp_cache_path = Path.cwd() / ".raccoon" / "libstp_step_cache.json"
        self._libstp_cache: List[Dict[str, Any]] = []
        self._libstp_last_error: Optional[str] = None
        self._libstp_last_indexed_at: Optional[str] = None
        self._libstp_lock = threading.Lock()
        self._load_libstp_cache()
        # In-memory cache for file-based discovery results
        self._library_steps_cache: Optional[List[StepFunction]] = None
        self._library_steps_ts: float = 0.0
        self._project_steps_cache: Dict[UUID, tuple[float, List[StepFunction]]] = {}

    def get_all_available_steps(self, project_uuid: UUID = None) -> List[Dict[str, Any]]:
        """Get all available steps for a project (library + scoped project steps)"""
        steps: List[StepFunction] = []
        steps.extend(self._default_library_steps())
        steps.extend(self._discover_library_files_cached())

        if project_uuid:
            steps.extend(self._get_project_specific_steps_cached(project_uuid))

        return self._deduplicate_steps(steps)

    def get_library_steps(self) -> List[Dict[str, Any]]:
        """Get only library steps (defaults + local helper modules)."""
        steps = self._default_library_steps()
        steps.extend(self._discover_library_files())
        return self._deduplicate_steps(steps)

    def get_project_steps(self, project_uuid: UUID) -> List[Dict[str, Any]]:
        """Get steps specific to a project"""
        return self._deduplicate_steps(self._get_project_specific_steps(project_uuid))

    def _discover_library_files_cached(self) -> List[StepFunction]:
        """Return library steps, re-scanning only when TTL has expired."""
        now = time.monotonic()
        if self._library_steps_cache is not None and (now - self._library_steps_ts) < self._DISCOVERY_TTL:
            return list(self._library_steps_cache)
        result = self._discover_library_files()
        self._library_steps_cache = result
        self._library_steps_ts = now
        return result

    def _get_project_specific_steps_cached(self, project_uuid: UUID) -> List[StepFunction]:
        """Return project steps, re-scanning only when TTL has expired."""
        now = time.monotonic()
        cached = self._project_steps_cache.get(project_uuid)
        if cached is not None and (now - cached[0]) < self._DISCOVERY_TTL:
            return list(cached[1])
        result = self._get_project_specific_steps(project_uuid)
        self._project_steps_cache[project_uuid] = (now, result)
        return result

    def _discover_library_files(self) -> List[StepFunction]:
        steps: List[StepFunction] = []
        steps.extend(self._discover_local_library_files())
        return steps

    def _discover_local_library_files(self) -> List[StepFunction]:
        project_root = Path.cwd()
        analyzer = DSLStepAnalyzer(project_root)
        library_files = analyzer._find_library_steps()
        for file_path in library_files:
            analyzer._analyze_file(file_path)
        return list(analyzer.discovered_steps)

    # Note: libstp discovery happens on the device (backendide).
    # This service imports steps from the device via import_libstp_cache().

    def _default_library_steps(self) -> List[StepFunction]:
        return self._cached_libstp_steps()

    def _get_project_specific_steps(self, project_uuid: UUID) -> List[StepFunction]:
        project = self.project_service.get_project(project_uuid)
        if not project:
            return []

        project_dir = self.project_service.get_project_path(project_uuid)
        if not project_dir.exists():
            return []

        analyzer = DSLStepAnalyzer(Path.cwd())
        scan_root = project_dir / "src" if (project_dir / "src").exists() else project_dir
        project_step_files = [
            path
            for path in scan_root.rglob("*.py")
            if "__pycache__" not in path.parts and ".venv" not in path.parts
        ]
        project_steps: List[StepFunction] = []
        for file_path in project_step_files:
            before_count = len(analyzer.discovered_steps)
            analyzer._analyze_file(file_path)
            project_steps.extend(analyzer.discovered_steps[before_count:])

        return project_steps

    def import_libstp_cache(self, steps: List[Dict[str, Any]], last_indexed_at: Optional[str] = None) -> None:
        """Replace the cached libstp step index with data fetched elsewhere."""
        cache_dir = self._libstp_cache_path.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "steps": steps,
            "last_indexed_at": last_indexed_at or datetime.now(timezone.utc).isoformat(),
        }
        self._libstp_cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with self._libstp_lock:
            self._libstp_cache = steps
            self._libstp_last_indexed_at = payload["last_indexed_at"]
            self._libstp_last_error = None

    def get_libstp_cache_status(self) -> Dict[str, Any]:
        """Return cache health metadata used by the IDE refresh workflow."""
        with self._libstp_lock:
            status = "ready" if self._libstp_cache else "empty"
            if self._libstp_last_error:
                status = "error"
            return {
                "status": status,
                "count": len(self._libstp_cache),
                "last_indexed_at": self._libstp_last_indexed_at,
                "error": self._libstp_last_error,
            }

    def clear_libstp_cache(self) -> None:
        """Drop the cached libstp step index from memory and disk."""
        with self._libstp_lock:
            self._clear_libstp_cache_locked()

    def _clear_libstp_cache_locked(self) -> None:
        self._libstp_cache = []
        self._libstp_last_indexed_at = None
        self._libstp_last_error = None
        try:
            if self._libstp_cache_path.exists():
                self._libstp_cache_path.unlink()
        except Exception:
            pass

    def _load_libstp_cache(self) -> None:
        if not self._libstp_cache_path.exists():
            return
        try:
            data = json.loads(self._libstp_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        steps = data.get("steps")
        if isinstance(steps, list):
            self._libstp_cache = [s for s in steps if isinstance(s, dict)]
        last_indexed_at = data.get("last_indexed_at")
        if isinstance(last_indexed_at, str):
            self._libstp_last_indexed_at = last_indexed_at

    def _cached_libstp_steps(self) -> List[StepFunction]:
        cached = self._libstp_cache
        if not cached:
            return []
        steps: List[StepFunction] = []
        for entry in cached:
            step = self._step_from_dict(entry)
            if step:
                steps.append(step)
        return steps

    def _step_from_dict(self, entry: Dict[str, Any]) -> Optional[StepFunction]:
        name = entry.get("name")
        import_path = entry.get("import")
        file_path = entry.get("file", "<cached>")
        if not name or not import_path:
            return None
        args = []
        for arg in entry.get("arguments", []) or []:
            if not isinstance(arg, dict):
                continue
            args.append(
                StepArgument(
                    name=arg.get("name"),
                    type_name=arg.get("type", "Any"),
                    type_import=arg.get("import"),
                    is_optional=bool(arg.get("optional", False)),
                    default_value=arg.get("default"),
                )
            )
        # Extract tags from cached entry
        tags_raw = entry.get("tags")
        tags = [t for t in tags_raw if isinstance(t, str)] if isinstance(tags_raw, list) else None
        return StepFunction(
            name=name,
            import_path=import_path,
            arguments=args,
            file_path=file_path,
            tags=tags if tags else None,
        )

    def _deduplicate_steps(self, steps: List[StepFunction]) -> List[Dict[str, Any]]:
        dedup: Dict[str, Dict[str, Any]] = {}
        for step in steps:
            payload = step.to_dict()
            key = f"{payload.get('import')}::{payload.get('name')}"
            dedup[key] = payload
        return list(dedup.values())
