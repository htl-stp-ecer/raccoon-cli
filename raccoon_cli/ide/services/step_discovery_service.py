"""Discover available DSL steps from the local Python environment."""

import importlib.util
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from uuid import UUID

from raccoon_cli.ide.core.analysis.step_analyzer import DSLStepAnalyzer, StepFunction
from raccoon_cli.ide.services.project_service import ProjectService

logger = logging.getLogger(__name__)


class StepDiscoveryService:
    """Discover DSL steps from the locally installed raccoon and project source."""

    def __init__(self, project_service: ProjectService):
        self.project_service = project_service
        self._library_steps: List[StepFunction] = self._scan_library_steps()
        logger.info("Indexed %d library steps on startup", len(self._library_steps))

    def get_all_available_steps(self, project_uuid: UUID = None) -> List[Dict[str, Any]]:
        """Get all available steps: library + project-specific."""
        steps: List[StepFunction] = list(self._library_steps)

        if project_uuid:
            steps.extend(self._scan_project_steps(project_uuid))

        return self._deduplicate(steps)

    def get_library_steps(self) -> List[Dict[str, Any]]:
        return self._deduplicate(self._library_steps)

    def get_project_steps(self, project_uuid: UUID) -> List[Dict[str, Any]]:
        return self._deduplicate(self._scan_project_steps(project_uuid))

    def get_raccoon_cache_status(self) -> Dict[str, Any]:
        return {
            "status": "ready" if self._library_steps else "empty",
            "count": len(self._library_steps),
            "last_indexed_at": None,
            "error": None,
        }

    def refresh_raccoon_cache_locally(self) -> Dict[str, Any]:
        """Force re-scan of the local raccoon install."""
        self._library_steps = self._scan_library_steps()
        logger.info("Re-indexed %d library steps", len(self._library_steps))
        return self.get_raccoon_cache_status()

    def clear_raccoon_cache(self) -> None:
        self._library_steps = []

    def import_raccoon_cache(self, steps: List[Dict[str, Any]], last_indexed_at: Optional[str] = None) -> None:
        """No-op — kept for API compatibility. Steps are discovered locally."""
        pass

    def _scan_library_steps(self) -> List[StepFunction]:
        """Scan both the installed raccoon package and any local raccoon/ dir."""
        steps: List[StepFunction] = []

        # Installed raccoon package (stubs or real)
        raccoon_dir = self._find_installed_raccoon_dir()
        if raccoon_dir:
            analyzer = DSLStepAnalyzer(raccoon_dir.parent)
            for f in analyzer._find_library_steps():
                analyzer._analyze_file(f)
            steps.extend(analyzer.discovered_steps)
            logger.debug("Found %d steps from installed raccoon at %s", len(analyzer.discovered_steps), raccoon_dir)

        # Local raccoon/ directory under cwd (if present)
        local_analyzer = DSLStepAnalyzer(Path.cwd())
        local_files = local_analyzer._find_library_steps()
        if local_files:
            for f in local_files:
                local_analyzer._analyze_file(f)
            steps.extend(local_analyzer.discovered_steps)
            logger.debug("Found %d steps from local raccoon/", len(local_analyzer.discovered_steps))

        return steps

    # ── Project steps ──────────────────────────────────────

    def _scan_project_steps(self, project_uuid: UUID) -> List[StepFunction]:
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
        steps: List[StepFunction] = []
        for file_path in project_step_files:
            before = len(analyzer.discovered_steps)
            analyzer._analyze_file(file_path)
            steps.extend(analyzer.discovered_steps[before:])
        return steps

    # ── Helpers ────────────────────────────────────────────

    def _find_installed_raccoon_dir(self) -> Optional[Path]:
        try:
            spec = importlib.util.find_spec("raccoon")
        except Exception:
            return None
        if spec is None:
            return None
        if spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                pkg = Path(location)
                if pkg.is_dir():
                    return pkg
        if spec.origin:
            pkg = Path(spec.origin).parent
            if pkg.is_dir():
                return pkg
        return None

    def _deduplicate(self, steps: List[StepFunction]) -> List[Dict[str, Any]]:
        dedup: Dict[str, Dict[str, Any]] = {}
        for step in steps:
            payload = step.to_dict()
            key = f"{payload.get('import')}::{payload.get('name')}"
            dedup[key] = payload
        return list(dedup.values())
