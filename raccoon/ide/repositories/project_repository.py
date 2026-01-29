import os
from pathlib import Path

import yaml
import uuid
from typing import Any, Callable, Dict, List, Optional

from raccoon.ide.schemas.project import ProjectCreate, ProjectInDB


class ProjectRepository:
    CONFIG_FILENAME = "raccoon.project.yml"

    def __init__(self, project_root: str | Path):
        self.project_root = str(project_root)
        os.makedirs(self.project_root, exist_ok=True)

        def uuid_constructor(loader, node):
            return uuid.UUID(loader.construct_scalar(node))

        yaml.SafeLoader.add_constructor(
            "tag:yaml.org,2002:python/object:uuid.UUID", uuid_constructor
        )

    def get_project_path(self, project_uuid: uuid.UUID) -> Path:
        return Path(os.path.join(self.project_root, str(project_uuid)))

    def get_project_config_path(self, project_uuid: uuid.UUID) -> Path:
        return self.get_project_path(project_uuid) / self.CONFIG_FILENAME

    def _load_project_config(self, project_uuid: uuid.UUID) -> Dict[str, Any] | None:
        config_path = self.get_project_config_path(project_uuid)
        if not config_path.exists():
            return None
        with config_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            return None
        return data

    def _write_project_config(self, project_uuid: uuid.UUID, data: Dict[str, Any]) -> None:
        config_path = self.get_project_config_path(project_uuid)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        def _normalize(obj: Dict[str, Any]) -> Dict[str, Any]:
            normalized: Dict[str, Any] = {}
            for key, value in obj.items():
                if isinstance(value, uuid.UUID):
                    normalized[key] = str(value)
                else:
                    normalized[key] = value
            return normalized

        with config_path.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(_normalize(data), stream, sort_keys=False)

    def read_project_config(self, project_uuid: uuid.UUID) -> Dict[str, Any]:
        return self._load_project_config(project_uuid) or {}

    def write_project_config(self, project_uuid: uuid.UUID, data: Dict[str, Any]) -> None:
        self._write_project_config(project_uuid, data)

    def update_project_config(
        self,
        project_uuid: uuid.UUID,
        mutate: Callable[[Dict[str, Any]], Dict[str, Any] | None],
    ) -> Optional[Dict[str, Any]]:
        current = self.read_project_config(project_uuid)
        updated = mutate(dict(current)) if mutate else current
        if updated is None:
            return None
        updated.setdefault("uuid", project_uuid)
        self._write_project_config(project_uuid, updated)
        return updated

    def create_project(self, project_create: ProjectCreate) -> ProjectInDB:
        new_uuid = uuid.uuid4()
        project_path = self.get_project_path(new_uuid)
        project_path.mkdir(parents=True, exist_ok=True)

        return ProjectInDB(uuid=new_uuid, name=project_create.name)

    def get_project(self, project_uuid: uuid.UUID) -> Optional[ProjectInDB]:
        data = self._load_project_config(project_uuid)
        if not data:
            return None

        name = data.get("name")
        uuid_value = data.get("uuid", project_uuid)
        if not name or not uuid_value:
            return None

        try:
            parsed_uuid = uuid_value if isinstance(uuid_value, uuid.UUID) else uuid.UUID(str(uuid_value))
        except (ValueError, TypeError):
            return None

        return ProjectInDB(uuid=parsed_uuid, name=name)

    def update_project(
        self, project_uuid: uuid.UUID, project_update: ProjectCreate
    ) -> Optional[ProjectInDB]:
        config = self._load_project_config(project_uuid)
        if not config:
            return None

        config["name"] = project_update.name
        config.setdefault("uuid", project_uuid)
        self._write_project_config(project_uuid, config)

        return ProjectInDB(uuid=project_uuid, name=project_update.name)

    def delete_project(self, project_uuid: uuid.UUID) -> bool:
        project_path = str(self.get_project_path(project_uuid))
        if os.path.exists(project_path):
            import shutil

            shutil.rmtree(project_path)
            return True
        return False

    def list_projects(self) -> List[ProjectInDB]:
        projects = []
        if not os.path.exists(self.project_root):
            return []

        for item_name in os.listdir(self.project_root):
            item_path = os.path.join(self.project_root, item_name)
            if os.path.isdir(item_path):
                try:
                    project_uuid = uuid.UUID(item_name)
                    project = self.get_project(project_uuid)
                    if project:
                        projects.append(project)
                except ValueError:
                    # Not a valid UUID folder, ignore
                    pass
        return projects
