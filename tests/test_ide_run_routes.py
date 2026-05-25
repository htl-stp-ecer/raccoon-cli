"""Smoke tests for the /api/v1/runs HTTP surface."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from raccoon_cli.ide.app import create_app


PROJECT_UUID = "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"


def _setup_project_with_run(tmp_path: Path) -> Path:
    project_dir = tmp_path / "Demo Bot"
    project_dir.mkdir()
    (project_dir / "raccoon.project.yml").write_text(
        f"name: Demo Bot\nuuid: {PROJECT_UUID}\n", encoding="utf-8"
    )
    run_dir = project_dir / ".raccoon/runs" / "20260523T143012Z"
    run_dir.mkdir(parents=True)
    lines = [
        json.dumps({"kind": "header", "format_version": 1, "started_at_unix_ns": 0}),
        json.dumps({"kind": "frame", "t_ns": 1_000_000}),
        json.dumps({"kind": "frame", "t_ns": 500_000_000}),
    ]
    (run_dir / "localization.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return project_dir


def test_list_runs_endpoint(tmp_path: Path):
    _setup_project_with_run(tmp_path)
    client = TestClient(create_app(project_root=tmp_path))
    response = client.get(f"/api/v1/runs/{PROJECT_UUID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["run_id"] == "20260523T143012Z"
    assert body[0]["has_localization"] is True


def test_metadata_endpoint_includes_lazy_fields(tmp_path: Path):
    _setup_project_with_run(tmp_path)
    client = TestClient(create_app(project_root=tmp_path))
    response = client.get(f"/api/v1/runs/{PROJECT_UUID}/20260523T143012Z/metadata")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frame_count"] == 2
    assert body["duration_ms"] == 500


def test_localization_streaming_endpoint(tmp_path: Path):
    _setup_project_with_run(tmp_path)
    client = TestClient(create_app(project_root=tmp_path))
    response = client.get(
        f"/api/v1/runs/{PROJECT_UUID}/20260523T143012Z/localization"
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]
    # JSONL: should split into 3 non-empty lines
    lines = [l for l in response.text.splitlines() if l.strip()]
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["kind"] == "header"


def test_delete_endpoint(tmp_path: Path):
    project_dir = _setup_project_with_run(tmp_path)
    client = TestClient(create_app(project_root=tmp_path))
    response = client.delete(f"/api/v1/runs/{PROJECT_UUID}/20260523T143012Z")
    assert response.status_code == 204, response.text
    assert not (project_dir / ".raccoon/runs" / "20260523T143012Z").exists()


def test_invalid_run_id_returns_404(tmp_path: Path):
    _setup_project_with_run(tmp_path)
    client = TestClient(create_app(project_root=tmp_path))
    response = client.get(f"/api/v1/runs/{PROJECT_UUID}/not-a-timestamp/metadata")
    assert response.status_code == 404
