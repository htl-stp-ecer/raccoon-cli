from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from raccoon_cli.ide.repositories.project_repository import ProjectRepository
from raccoon_cli.ide.schemas.project import ProjectCreate


def test_create_project_invokes_raccoon_cli_and_returns_scaffolded_project(tmp_path: Path):
    repository = ProjectRepository(tmp_path)
    project_dir = tmp_path / "Demo Bot"

    def fake_run(*args, **kwargs):
        project_dir.mkdir()
        (project_dir / "raccoon.project.yml").write_text(
            "name: Demo Bot\nuuid: 62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1\n",
            encoding="utf-8",
        )
        return CompletedProcess(args=args[0], returncode=0, stdout="created", stderr="")

    with patch("raccoon_cli.ide.repositories.project_repository.subprocess.run", side_effect=fake_run) as run_mock:
        project = repository.create_project(ProjectCreate(name="Demo Bot"))

    run_mock.assert_called_once_with(
        [
            "raccoon",
            "create",
            "project",
            "Demo Bot",
            "--path",
            str(tmp_path),
            "--no-wizard",
            "--no-open-ide",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert project.name == "Demo Bot"
    assert str(project.uuid) == "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"
