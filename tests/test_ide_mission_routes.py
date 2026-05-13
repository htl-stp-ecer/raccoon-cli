from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from fastapi.testclient import TestClient

from raccoon_cli.ide.app import create_app


def test_create_mission_route_uses_shared_cli_creation_path(tmp_path: Path):
    project_uuid = "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"
    project_dir = tmp_path / "Demo Bot"
    project_dir.mkdir()
    (project_dir / "raccoon.project.yml").write_text(
        f"name: Demo Bot\nuuid: {project_uuid}\n",
        encoding="utf-8",
    )
    (project_dir / "src").mkdir()
    (project_dir / "src" / "missions").mkdir(parents=True)

    with patch("raccoon_cli.ide.repositories.project_repository.subprocess.run") as run_mock:
        run_mock.return_value = CompletedProcess(args=[], returncode=0, stdout="created", stderr="")
        client = TestClient(create_app(project_root=tmp_path))
        response = client.post(f"/api/v1/missions/{project_uuid}", json={"name": "Drive"})

    assert response.status_code == 200, response.text
    run_mock.assert_called_once_with(
        ["raccoon", "create", "mission", "Drive"],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
