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


def test_save_config_keys_preserves_split_config_layout(tmp_path: Path):
    repository = ProjectRepository(tmp_path)
    project_uuid = UUID("62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1")
    project_dir = tmp_path / "Demo Bot"
    config_dir = project_dir / "config"
    config_dir.mkdir(parents=True)

    (project_dir / "raccoon.project.yml").write_text(
        "\n".join([
            "name: Demo Bot",
            f"uuid: {project_uuid}",
            "robot: !include 'config/robot.yml'",
            "definitions: !include 'config/hardware.yml'",
            "",
        ]),
        encoding="utf-8",
    )
    (config_dir / "robot.yml").write_text(
        "\n".join([
            "physical:",
            "  width_cm: 15.0",
            "  length_cm: 20.0",
            "  sensors: []",
            "",
        ]),
        encoding="utf-8",
    )
    (config_dir / "hardware.yml").write_text(
        "\n".join([
            "front_left_ir_sensor:",
            "  type: IRSensor",
            "  port: 1",
            "_motors: !include-merge 'motors.yml'",
            "",
        ]),
        encoding="utf-8",
    )
    (config_dir / "motors.yml").write_text(
        "\n".join([
            "left_motor:",
            "  type: Motor",
            "  port: 0",
            "",
        ]),
        encoding="utf-8",
    )

    config = repository.read_project_config(project_uuid)
    config["robot"]["physical"]["sensors"] = [
        {"name": "front_left_ir_sensor", "x_cm": 4.0, "y_cm": 12.0}
    ]
    repository.save_config_keys(project_uuid, {"robot": config["robot"]})

    assert "_motors: !include-merge 'motors.yml'" in (config_dir / "hardware.yml").read_text(encoding="utf-8")
    assert "left_motor:" in (config_dir / "motors.yml").read_text(encoding="utf-8")
    assert "front_left_ir_sensor:" in (config_dir / "hardware.yml").read_text(encoding="utf-8")
    assert "robot: !include 'config/robot.yml'" in (project_dir / "raccoon.project.yml").read_text(encoding="utf-8")
    assert "width_cm: 15.0" in (config_dir / "robot.yml").read_text(encoding="utf-8")
    assert "x_cm: 4.0" in (config_dir / "robot.yml").read_text(encoding="utf-8")
