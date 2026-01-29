from typing import List, Optional
from uuid import UUID

from raccoon.ide.core.project_code_gen import ProjectCodeGen
from raccoon.ide.repositories.project_repository import ProjectRepository
from raccoon.ide.schemas.project import ProjectCreate, ProjectInDB


class ProjectService:
    def __init__(
        self,
        project_repository: ProjectRepository,
        project_code_gen: ProjectCodeGen,
    ):
        self.project_repository = project_repository
        self.project_code_gen = project_code_gen

    def create_project(self, project_create: ProjectCreate) -> ProjectInDB:
        """Create a new project entry.

        Note: This only creates the project entry in the repository.
        Project scaffolding should be done via 'raccoon create project'.
        """
        project = self.project_repository.create_project(project_create)
        if not project:
            raise ValueError("Failed to create project")
        return project

    def get_project(self, project_uuid: UUID) -> Optional[ProjectInDB]:
        return self.project_repository.get_project(project_uuid)

    def update_project(
            self, project_uuid: UUID, project_update: ProjectCreate
    ) -> Optional[ProjectInDB]:
        return self.project_repository.update_project(project_uuid, project_update)

    def delete_project(self, project_uuid: UUID) -> bool:
        return self.project_repository.delete_project(project_uuid)

    def list_projects(self) -> List[ProjectInDB]:
        return self.project_repository.list_projects()
