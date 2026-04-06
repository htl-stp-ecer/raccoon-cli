"""Project code generation using raccoon's shared helpers."""

from pathlib import Path
from uuid import UUID

from raccoon.ide.core.naming import NormalizedName
from raccoon.ide.repositories.project_repository import ProjectRepository
from raccoon.mission_codegen import (
    get_templates_dir,
    copy_template_dir,
    add_mission_import_to_main,
    remove_mission_import_from_main,
)
from raccoon.mission_config import add_mission_to_config, remove_mission_from_config


class ProjectCodeGen:
    """Code generation for projects and missions using raccoon shared helpers."""

    def __init__(
        self,
        project_repository: ProjectRepository,
        template_root: Path | str = None,
    ):
        self.project_repository = project_repository
        if template_root is None:
            template_root = get_templates_dir()
        self.template_root = Path(template_root)

    def add_mission_to_project(
        self,
        project_uuid: UUID,
        mission_name: NormalizedName,
    ) -> None:
        """Add a new mission to a project.

        Args:
            project_uuid: The project UUID
            mission_name: Normalized mission name

        Raises:
            FileExistsError: If mission already exists
            FileNotFoundError: If project not found
        """
        project_path = self.project_repository.get_project_path(project_uuid)
        if not project_path:
            raise FileNotFoundError("Project not found")

        # Check if mission already exists
        missions_dir = project_path / "src" / "missions"
        expected_mission_file = missions_dir / f"{mission_name.snake}_mission.py"

        if expected_mission_file.exists():
            raise FileExistsError(f"Mission file {expected_mission_file.name} already exists")

        # Use raccoon's template copy
        import datetime
        template_path = self.template_root / "mission"
        context = {
            "mission_snake_case": mission_name.snake,
            "mission_pascal_case": mission_name.pascal,
            "project_name": project_path.name,
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        copy_template_dir(template_path, project_path, context)

        # Add import to main.py
        add_mission_import_to_main(project_path, mission_name.snake, mission_name.pascal)

        # Add to project config
        mission_class_name = f"{mission_name.pascal}Mission"
        add_mission_to_config(project_path, mission_class_name)

    def remove_mission_from_project(
        self,
        project_uuid: UUID,
        mission_snake: str,
        mission_pascal: str,
        delete_file: bool = True,
    ) -> bool:
        """Remove a mission from a project.

        Args:
            project_uuid: The project UUID
            mission_snake: Snake case mission name (without _mission suffix)
            mission_pascal: Pascal case mission name (without Mission suffix)
            delete_file: Whether to delete the mission file

        Returns:
            True if mission was removed
        """
        project_path = self.project_repository.get_project_path(project_uuid)
        if not project_path:
            return False

        mission_class = f"{mission_pascal}Mission"

        # Remove from config
        remove_mission_from_config(project_path, mission_class)

        # Remove import from main.py
        remove_mission_import_from_main(project_path, mission_snake, mission_pascal)

        # Delete file if requested
        if delete_file:
            mission_file = project_path / "src" / "missions" / f"{mission_snake}_mission.py"
            if mission_file.exists():
                mission_file.unlink()

        return True
