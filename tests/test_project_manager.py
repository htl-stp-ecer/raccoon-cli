from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from raccoon.server.services.project_manager import ProjectManager


def test_create_project_invokes_raccoon_cli(tmp_path: Path):
    manager = ProjectManager(tmp_path)
    project_dir = tmp_path / "Demo Bot"

    def fake_run(*args, **kwargs):
        project_dir.mkdir()
        (project_dir / "raccoon.project.yml").write_text(
            "name: Demo Bot\nuuid: demo-uuid\n",
            encoding="utf-8",
        )
        return CompletedProcess(args=args[0], returncode=0, stdout="created", stderr="")

    with patch("raccoon.server.services.project_manager.subprocess.run", side_effect=fake_run) as run_mock:
        project = manager.create_project("Demo Bot")

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
    assert project["name"] == "Demo Bot"
    assert project["id"] == "demo-uuid"
    assert project["path"] == project_dir


def test_create_project_rejects_invalid_name(tmp_path: Path):
    manager = ProjectManager(tmp_path)

    with patch("raccoon.server.services.project_manager.subprocess.run") as run_mock:
        with pytest.raises(ValueError):
            manager.create_project("bad/name")

    run_mock.assert_not_called()
