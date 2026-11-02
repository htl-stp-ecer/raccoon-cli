from pathlib import Path
from unittest.mock import patch

import pytest

from raccoon_cli.server.services.project_manager import ProjectManager


def test_create_project_uses_scaffold_service(tmp_path: Path):
    manager = ProjectManager(tmp_path)
    project_dir = tmp_path / "Demo Bot"

    def fake_scaffold(name, target_dir):
        target_dir.mkdir()
        (target_dir / "raccoon.project.yml").write_text(
            "name: Demo Bot\nuuid: demo-uuid\n",
            encoding="utf-8",
        )
        return "demo-uuid", None

    with patch(
        "raccoon_cli.server.services.project_manager.scaffold_project", side_effect=fake_scaffold
    ) as scaffold_mock:
        project = manager.create_project("Demo Bot")

    scaffold_mock.assert_called_once_with("Demo Bot", project_dir)
    assert project["name"] == "Demo Bot"
    assert project["id"] == "demo-uuid"
    assert project["path"] == project_dir


def test_create_project_rejects_invalid_name(tmp_path: Path):
    manager = ProjectManager(tmp_path)

    with patch("raccoon_cli.server.services.project_manager.scaffold_project") as scaffold_mock:
        with pytest.raises(ValueError):
            manager.create_project("bad/name")

    scaffold_mock.assert_not_called()
