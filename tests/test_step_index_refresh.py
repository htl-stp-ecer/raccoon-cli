from pathlib import Path

from fastapi.testclient import TestClient

from raccoon_cli.ide.app import create_app
from raccoon_cli.ide.services.step_discovery_service import StepDiscoveryService


class _DummyProjectService:
    def get_project(self, project_uuid):
        return None

    def get_project_path(self, project_uuid):
        return Path(".")


def test_refresh_raccoon_cache_locally_indexes_installed_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    raccoon_dir = tmp_path / "site-packages" / "raccoon"
    step_file = raccoon_dir / "step" / "motion" / "drive_dsl.py"
    step_file.parent.mkdir(parents=True)
    step_file.write_text(
        """
from raccoon import dsl

@dsl
def local_drive(speed: float = 0.5):
    return speed
""".strip(),
        encoding="utf-8",
    )

    service = StepDiscoveryService(project_service=_DummyProjectService())
    monkeypatch.setattr(service, "_find_installed_raccoon_dir", lambda: raccoon_dir)

    status = service.refresh_raccoon_cache_locally()
    steps = service.get_library_steps()

    assert status["status"] == "ready"
    assert status["count"] == 1
    assert any(step["name"] == "local_drive" for step in steps)


def test_refresh_route_allows_local_indexing_without_device_url(tmp_path, monkeypatch):
    def fake_refresh(self):
        return {
            "status": "ready",
            "count": 7,
            "last_indexed_at": "2026-03-25T10:00:00+00:00",
            "error": None,
        }

    monkeypatch.setattr(StepDiscoveryService, "refresh_raccoon_cache_locally", fake_refresh)

    client = TestClient(create_app(project_root=tmp_path))
    response = client.post("/api/v1/steps/index/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["count"] == 7
