import importlib.util
from pathlib import Path
from typing import List, Dict, Any
from uuid import UUID

from raccoon.ide.core.analysis.step_analyzer import DSLStepAnalyzer, StepFunction, StepArgument
from raccoon.ide.services.project_service import ProjectService
from raccoon.ide.services.step_catalog import DEFAULT_LIBRARY_STEPS


class StepDiscoveryService:
    """Service for discovering and returning available DSL steps"""

    def __init__(self, project_service: ProjectService):
        self.project_service = project_service

    def get_all_available_steps(self, project_uuid: UUID = None) -> List[Dict[str, Any]]:
        """Get all available steps for a project (library + scoped project steps)"""
        steps: List[StepFunction] = []
        steps.extend(self._default_library_steps())
        steps.extend(self._discover_library_files())

        if project_uuid:
            steps.extend(self._get_project_specific_steps(project_uuid))

        return self._deduplicate_steps(steps)

    def get_library_steps(self) -> List[Dict[str, Any]]:
        """Get only library steps (defaults + local helper modules)."""
        steps = self._default_library_steps()
        steps.extend(self._discover_library_files())
        return self._deduplicate_steps(steps)

    def get_project_steps(self, project_uuid: UUID) -> List[Dict[str, Any]]:
        """Get steps specific to a project"""
        return self._deduplicate_steps(self._get_project_specific_steps(project_uuid))

    def _discover_library_files(self) -> List[StepFunction]:
        steps: List[StepFunction] = []
        steps.extend(self._discover_local_library_files())
        steps.extend(self._discover_libstp_package_steps())
        return steps

    def _discover_local_library_files(self) -> List[StepFunction]:
        project_root = Path.cwd()
        analyzer = DSLStepAnalyzer(project_root)
        library_files = analyzer._find_library_steps()
        for file_path in library_files:
            analyzer._analyze_file(file_path)
        return list(analyzer.discovered_steps)

    def _discover_libstp_package_steps(self) -> List[StepFunction]:
        """Discover step factory functions inside an installed libstp package."""
        spec = importlib.util.find_spec("libstp")
        if not spec:
            return []

        package_locations = spec.submodule_search_locations or []
        if not package_locations:
            return []

        discovered: List[StepFunction] = []
        for location in package_locations:
            location_path = Path(location)
            if not location_path.exists():
                continue

            step_dir = location_path / "step"
            if not step_dir.exists():
                continue

            analyzer = DSLStepAnalyzer(location_path.parent)
            for file_path in step_dir.rglob("*.py"):
                analyzer._analyze_file(file_path)
            discovered.extend(analyzer.discovered_steps)

        return discovered

    def _default_library_steps(self) -> List[StepFunction]:
        catalog: List[StepFunction] = []
        for entry in DEFAULT_LIBRARY_STEPS:
            arguments = [
                StepArgument(
                    name=arg.get("name"),
                    type_name=arg.get("type", "Any"),
                    type_import=None,
                    is_optional=bool(arg.get("optional", False)),
                    default_value=arg.get("default"),
                )
                for arg in entry.get("arguments", [])
            ]
            catalog.append(
                StepFunction(
                    name=entry["name"],
                    import_path=entry["import"],
                    arguments=arguments,
                    file_path=entry.get("file", "<builtin>"),
                )
            )
        return catalog

    def _get_project_specific_steps(self, project_uuid: UUID) -> List[StepFunction]:
        project = self.project_service.get_project(project_uuid)
        if not project:
            return []

        project_dir = Path.cwd() / "projects" / str(project_uuid)
        if not project_dir.exists():
            return []

        analyzer = DSLStepAnalyzer(Path.cwd())
        project_step_files = list(project_dir.rglob("*step*.py"))
        project_steps: List[StepFunction] = []
        for file_path in project_step_files:
            before_count = len(analyzer.discovered_steps)
            analyzer._analyze_file(file_path)
            project_steps.extend(analyzer.discovered_steps[before_count:])

        return project_steps

    def _deduplicate_steps(self, steps: List[StepFunction]) -> List[Dict[str, Any]]:
        dedup: Dict[str, Dict[str, Any]] = {}
        for step in steps:
            payload = step.to_dict()
            key = f"{payload.get('import')}::{payload.get('name')}"
            dedup[key] = payload
        return list(dedup.values())
