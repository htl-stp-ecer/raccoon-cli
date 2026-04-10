"""Project management service."""

import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("raccoon")
PROJECT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\s]+$")


class ProjectManager:
    """Manages Raccoon projects on the Pi."""

    def __init__(self, projects_dir: Path):
        self.projects_dir = projects_dir
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def list_projects(self) -> list[dict]:
        """
        List all projects in the projects directory.

        Returns list of project info dictionaries.
        """
        projects = []

        if not self.projects_dir.exists():
            return projects

        for item in self.projects_dir.iterdir():
            if not item.is_dir():
                continue

            project_info = self._get_project_info(item)
            if project_info:
                projects.append(project_info)

        # Sort by last modified, most recent first
        projects.sort(key=lambda p: p.get("last_modified", ""), reverse=True)
        return projects

    def get_project(self, project_id: str) -> Optional[dict]:
        """
        Get information about a specific project.

        Args:
            project_id: Project UUID or directory name

        Returns:
            Project info dict or None if not found
        """
        # First, try to find by UUID in config files
        for item in self.projects_dir.iterdir():
            if not item.is_dir():
                continue

            config_path = item / "raccoon.project.yml"
            if config_path.exists():
                try:
                    from raccoon_cli.yaml_utils import load_yaml
                    config = load_yaml(config_path)
                    if config.get("uuid") == project_id:
                        return self._get_project_info(item)
                except Exception:
                    pass

        # Fall back to directory name match
        project_path = self.projects_dir / project_id
        if project_path.exists() and project_path.is_dir():
            return self._get_project_info(project_path)

        return None

    def get_project_path(self, project_id: str) -> Optional[Path]:
        """
        Get the filesystem path for a project.

        Args:
            project_id: Project UUID or directory name

        Returns:
            Path to project directory or None if not found
        """
        project = self.get_project(project_id)
        return project["path"] if project else None

    def delete_project(self, project_id: str) -> bool:
        """
        Delete a project directory.

        Args:
            project_id: Project UUID or directory name

        Returns:
            True if deleted, False if not found
        """
        project = self.get_project(project_id)
        if not project:
            return False

        project_path = project["path"]
        if project_path.exists():
            shutil.rmtree(project_path)
            return True

        return False

    def create_project(self, name: str) -> dict:
        """
        Create a new project via the raccoon CLI.

        Args:
            name: Display name and directory name for the new project

        Returns:
            Project info dict for the created project
        """
        project_name = name.strip()
        if not project_name:
            raise ValueError("Project name cannot be empty")
        if len(project_name) > 100 or not PROJECT_NAME_PATTERN.fullmatch(project_name):
            raise ValueError("Project name may only contain letters, numbers, spaces, underscores, and hyphens")

        project_path = self.projects_dir / project_name
        if project_path.exists():
            raise FileExistsError(f"Project '{project_name}' already exists")

        try:
            result = subprocess.run(
                [
                    "raccoon",
                    "create",
                    "project",
                    project_name,
                    "--path",
                    str(self.projects_dir),
                    "--no-wizard",
                    "--no-open-ide",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("The 'raccoon' command is not available on the server") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            message = stderr or stdout or f"Failed to create project '{project_name}'"
            raise RuntimeError(message) from exc

        project = self._get_project_info(project_path)
        if not project:
            stdout = (result.stdout or "").strip()
            raise RuntimeError(stdout or f"Project '{project_name}' was created but could not be loaded")

        return project

    def create_project_dir(self, project_id: str) -> Path:
        """
        Create a directory for a new project.

        Args:
            project_id: Project UUID or desired directory name

        Returns:
            Path to created directory
        """
        project_path = self.projects_dir / project_id
        project_path.mkdir(parents=True, exist_ok=True)
        return project_path

    def _get_project_info(self, project_path: Path) -> Optional[dict]:
        """
        Extract project information from a directory.

        Args:
            project_path: Path to project directory

        Returns:
            Project info dict or None if invalid
        """
        config_path = project_path / "raccoon.project.yml"
        has_config = config_path.exists()

        # Try to load config for name and UUID
        name = project_path.name
        project_id = project_path.name

        if has_config:
            try:
                from raccoon_cli.yaml_utils import load_yaml
                config = load_yaml(config_path)
                name = config.get("name", project_path.name)
                project_id = config.get("uuid", project_path.name)
            except Exception:
                pass

        # Get last modified time
        try:
            mtime = config_path.stat().st_mtime if has_config else project_path.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            last_modified = None

        return {
            "id": project_id,
            "name": name,
            "path": project_path,
            "has_config": has_config,
            "last_modified": last_modified,
        }
