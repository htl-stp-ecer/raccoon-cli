"""Tests for the fingerprint + sync_state endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raccoon_cli.fingerprint import compute_fingerprint, default_exclude_patterns
from raccoon_cli.server import app as app_module
from raccoon_cli.server.app import create_app
from raccoon_cli.server.config import ServerConfig


@pytest.fixture
def server_client(tmp_path: Path, monkeypatch):
    """Spin up a TestClient against a FastAPI app pointed at ``tmp_path``.

    Also stubs out API-token auth so we can exercise POST without fetching one.
    """
    # Build a fresh server config and force the module-global to use it.
    config = ServerConfig(projects_dir=tmp_path / "programs", api_token="test-token")
    config.projects_dir.mkdir(parents=True, exist_ok=True)
    app_module._config = config  # bypass load_config()

    # The auth dependency re-reads the token from disk; stub it to match.
    from raccoon_cli.server import auth as auth_module

    monkeypatch.setattr(auth_module, "get_or_create_api_token", lambda: "test-token")

    app = create_app(config=config)
    with TestClient(app) as client:
        yield client, config

    app_module._config = None  # reset for other tests


def _make_project(projects_dir: Path, project_id: str) -> Path:
    """Create a minimal project the server can discover by directory name."""
    project_path = projects_dir / project_id
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "raccoon.project.yml").write_text(
        f"name: {project_id}\nuuid: {project_id}\n"
    )
    (project_path / "src").mkdir()
    (project_path / "src" / "main.py").write_text("print('hi')\n")
    return project_path


class TestFingerprintEndpoint:
    def test_returns_root_hash_matching_local_compute(self, server_client):
        client, config = server_client
        project_path = _make_project(config.projects_dir, "proj-a")

        response = client.get("/api/v1/projects/proj-a/fingerprint")
        assert response.status_code == 200
        data = response.json()

        local = compute_fingerprint(
            project_path, exclude_patterns=default_exclude_patterns()
        )
        assert data["root_hash"] == local.root_hash
        assert data["file_count"] == local.file_count

    def test_unknown_project_returns_404(self, server_client):
        client, _ = server_client
        response = client.get("/api/v1/projects/nope/fingerprint")
        assert response.status_code == 404

    def test_files_endpoint_returns_per_file_hashes(self, server_client):
        client, config = server_client
        _make_project(config.projects_dir, "proj-b")

        response = client.get("/api/v1/projects/proj-b/fingerprint/files")
        assert response.status_code == 200
        files = response.json()["files"]
        assert "raccoon.project.yml" in files
        assert "src/main.py" in files


class TestSyncStateEndpoint:
    def test_defaults_to_version_zero_before_any_sync(self, server_client):
        client, config = server_client
        _make_project(config.projects_dir, "proj-c")

        response = client.get("/api/v1/projects/proj-c/sync_state")
        assert response.status_code == 200
        assert response.json()["version"] == 0

    def test_update_bumps_counter_and_persists(self, server_client):
        client, config = server_client
        project_path = _make_project(config.projects_dir, "proj-d")

        local = compute_fingerprint(
            project_path, exclude_patterns=default_exclude_patterns()
        )

        response = client.post(
            "/api/v1/projects/proj-d/sync_state",
            headers={"X-API-Token": "test-token"},
            json={
                "fingerprint": local.root_hash,
                "expected_prev_version": 0,
                "synced_by": "tester@host",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["version"] == 1
        assert data["fingerprint"] == local.root_hash

        follow_up = client.get("/api/v1/projects/proj-d/sync_state").json()
        assert follow_up["version"] == 1

    def test_update_rejects_stale_expected_prev_version(self, server_client):
        client, config = server_client
        project_path = _make_project(config.projects_dir, "proj-e")

        local = compute_fingerprint(
            project_path, exclude_patterns=default_exclude_patterns()
        )
        # First bump succeeds
        first = client.post(
            "/api/v1/projects/proj-e/sync_state",
            headers={"X-API-Token": "test-token"},
            json={"fingerprint": local.root_hash, "expected_prev_version": 0},
        )
        assert first.status_code == 200

        # Second bump with the wrong expected version is a 409
        response = client.post(
            "/api/v1/projects/proj-e/sync_state",
            headers={"X-API-Token": "test-token"},
            json={"fingerprint": local.root_hash, "expected_prev_version": 0},
        )
        assert response.status_code == 409

    def test_update_rejects_fingerprint_mismatch(self, server_client):
        client, config = server_client
        _make_project(config.projects_dir, "proj-f")

        fake_hash = "0" * 64
        response = client.post(
            "/api/v1/projects/proj-f/sync_state",
            headers={"X-API-Token": "test-token"},
            json={"fingerprint": fake_hash, "expected_prev_version": 0},
        )
        assert response.status_code == 409

    def test_update_requires_auth(self, server_client):
        client, config = server_client
        project_path = _make_project(config.projects_dir, "proj-g")

        local = compute_fingerprint(
            project_path, exclude_patterns=default_exclude_patterns()
        )
        response = client.post(
            "/api/v1/projects/proj-g/sync_state",
            json={"fingerprint": local.root_hash, "expected_prev_version": 0},
        )
        # Missing header → 422 (pydantic) or 401 depending on fastapi version;
        # the important thing is the call does NOT succeed without a token.
        assert response.status_code in (401, 422)

    def test_update_excludes_the_sync_state_file_from_fingerprint(self, server_client):
        """Writing sync_state.json must not change the fingerprint on the next call."""
        client, config = server_client
        project_path = _make_project(config.projects_dir, "proj-h")

        before = client.get("/api/v1/projects/proj-h/fingerprint").json()["root_hash"]

        client.post(
            "/api/v1/projects/proj-h/sync_state",
            headers={"X-API-Token": "test-token"},
            json={"fingerprint": before, "expected_prev_version": 0},
        )
        assert (project_path / ".raccoon" / "sync_state.json").exists()

        after = client.get("/api/v1/projects/proj-h/fingerprint").json()["root_hash"]
        assert before == after
