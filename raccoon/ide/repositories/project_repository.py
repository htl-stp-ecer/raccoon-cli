"""Filesystem-backed repository for IDE project metadata and config files."""

import os
from pathlib import Path

import yaml
import uuid
from typing import Any, Callable, Dict, List, Optional

from raccoon.ide.schemas.project import ProjectCreate, ProjectInDB, ProjectConnection


class ProjectRepository:
    """Persist and discover project records rooted under a workspace directory."""

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
        uuid_path = Path(os.path.join(self.project_root, str(project_uuid)))
        if uuid_path.exists():
            return uuid_path
        resolved = self._find_project_path_by_uuid(project_uuid)
        if resolved:
            return resolved
        return uuid_path

    def get_project_config_path(self, project_uuid: uuid.UUID) -> Path:
        project_path = self.get_project_path(project_uuid)
        return project_path / self.CONFIG_FILENAME

    def _load_project_config(self, project_uuid: uuid.UUID) -> Dict[str, Any] | None:
        from raccoon.yaml_utils import load_yaml

        config_path = self.get_project_config_path(project_uuid)
        if not config_path.exists():
            return None
        data = load_yaml(config_path)
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

        from raccoon.yaml_utils import save_yaml

        save_yaml(_normalize(data), config_path)

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
        return self._project_from_config(data, fallback_uuid=project_uuid)

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

        for project_dir in self._iter_project_dirs():
            config_path = project_dir / self.CONFIG_FILENAME
            if not config_path.exists():
                continue
            try:
                data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            project = self._project_from_config(data, fallback_uuid=None)
            if project:
                projects.append(project)
        return projects

    def _iter_project_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        for root, dirnames, _ in os.walk(self.project_root):
            for dirname in dirnames:
                candidate = Path(root) / dirname
                if (candidate / self.CONFIG_FILENAME).exists():
                    dirs.append(candidate)
        return dirs

    def _find_project_path_by_uuid(self, project_uuid: uuid.UUID) -> Optional[Path]:
        for project_dir in self._iter_project_dirs():
            config_path = project_dir / self.CONFIG_FILENAME
            if not config_path.exists():
                continue
            try:
                data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            uuid_value = data.get("uuid")
            try:
                parsed_uuid = uuid_value if isinstance(uuid_value, uuid.UUID) else uuid.UUID(str(uuid_value))
            except (ValueError, TypeError):
                continue
            if parsed_uuid == project_uuid:
                return project_dir
        return None

    def _project_from_config(
        self,
        data: Dict[str, Any],
        fallback_uuid: uuid.UUID | None,
    ) -> Optional[ProjectInDB]:
        name = data.get("name")
        uuid_value = data.get("uuid", fallback_uuid)
        if not name or not uuid_value:
            return None
        try:
            parsed_uuid = uuid_value if isinstance(uuid_value, uuid.UUID) else uuid.UUID(str(uuid_value))
        except (ValueError, TypeError):
            return None

        connection = None
        connection_payload = data.get("connection")
        if isinstance(connection_payload, dict):
            connection = ProjectConnection(
                pi_address=connection_payload.get("pi_address"),
                pi_port=connection_payload.get("pi_port"),
                pi_user=connection_payload.get("pi_user"),
                remote_path=connection_payload.get("remote_path"),
                auto_connect=connection_payload.get("auto_connect"),
            )

        return ProjectInDB(uuid=parsed_uuid, name=name, connection=connection)
