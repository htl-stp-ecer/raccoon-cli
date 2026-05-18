from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from raccoon_cli.ide.repositories.project_repository import ProjectRepository
from raccoon_cli.ide.schemas.project import ProjectCreate


def test_create_project_calls_scaffold_project_and_returns_project(tmp_path: Path):
    repository = ProjectRepository(tmp_path)
    project_dir = tmp_path / "Demo Bot"

    def fake_scaffold(name, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "raccoon.project.yml").write_text(
            "name: Demo Bot\nuuid: 62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1\n",
            encoding="utf-8",
        )
        from raccoon_cli.git_history import GitInitResult
        return "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1", GitInitResult(
            initialized=True, commit_created=False, reason="no_changes"
        )

    with patch("raccoon_cli.ide.repositories.project_repository.scaffold_project", side_effect=fake_scaffold) as mock_scaffold:
        project = repository.create_project(ProjectCreate(name="Demo Bot"))

    mock_scaffold.assert_called_once_with("Demo Bot", project_dir)
    assert project.name == "Demo Bot"
    assert str(project.uuid) == "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"


def test_create_mission_calls_shared_create_mission(tmp_path: Path):
    repository = ProjectRepository(tmp_path)
    project_uuid = UUID("62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1")
    project_dir = tmp_path / "Demo Bot"
    project_dir.mkdir()
    (project_dir / "raccoon.project.yml").write_text(
        f"name: Demo Bot\nuuid: {project_uuid}\n",
        encoding="utf-8",
    )

    with patch("raccoon_cli.ide.repositories.project_repository._create_mission") as mock_create:
        mock_create.return_value = "M010DriveMission"
        repository.create_mission(project_uuid, "Drive")

    mock_create.assert_called_once_with(project_dir, "Drive")
