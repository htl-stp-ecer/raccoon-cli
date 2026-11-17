import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raccoon_cli import camera_discovery
from raccoon_cli.server import app as app_module
from raccoon_cli.server.app import create_app
from raccoon_cli.server.config import ServerConfig

needs_raccoon_cam = pytest.mark.skipif(
    importlib.util.find_spec("raccoon_cam") is None, reason="raccoon-cam is not installed here"
)

BACKEND = '''
from raccoon_cam import CamBackend, Option

class Mine(CamBackend):
    """My own camera."""
    name = "mine"
    allow_processors = True
    provides_detections = False
    options = (Option("gain", "float", 1.0, min=0, max=8),)
    def start(self, sink): pass
    def stop(self): pass
'''


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    config = ServerConfig(projects_dir=tmp_path / "programs", api_token="test-token")
    config.projects_dir.mkdir(parents=True, exist_ok=True)
    app_module._config = config
    from raccoon_cli.server import auth as auth_module

    monkeypatch.setattr(auth_module, "get_or_create_api_token", lambda: "test-token")
    monkeypatch.setenv("RACCOON_CAM_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app(config=config)) as client:
        yield client, config
    app_module._config = None


@pytest.fixture
def project(server):
    _, config = server
    root = config.projects_dir / "my-robot"
    (root / "src").mkdir(parents=True)
    (root / "raccoon.project.yml").write_text("name: My Robot\nuuid: uuid-1\n")
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "mine.py").write_text(BACKEND)
    return root


def auth():
    return {"X-API-Token": "test-token"}


def fake_builtin(monkeypatch):
    monkeypatch.setattr(
        camera_discovery, "builtin_backends",
        lambda timeout=0: [{"path": "raccoon_cam.backends.usb:UsbCameraBackend", "name": "usb", "description": "USB", "options": []}],
    )


def test_the_list_has_the_built_in_and_the_project_backends_with_where_they_come_from(server, project, monkeypatch):
    client, _ = server
    fake_builtin(monkeypatch)
    response = client.get("/api/v1/camera/backends")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    usb, mine = body["backends"]
    assert usb["origin"] == {"kind": "builtin", "project": None}
    assert mine["origin"] == {"kind": "project", "project": "My Robot"}
    assert (mine["path"], mine["project_id"], mine["name"]) == ("src.mine:Mine", "uuid-1", "mine")


def test_a_backend_is_described_with_its_origin(server, project, monkeypatch):
    client, config = server
    seen = {}

    def describe(path, project_dir=None, kind="backend", timeout=0):
        seen.update(path=path, project_dir=project_dir)
        return {"name": "mine", "options": [{"name": "gain"}]}

    monkeypatch.setattr(camera_discovery, "describe_class", describe)
    response = client.get("/api/v1/camera/backends/describe", params={"path": "src.mine:Mine", "project_id": "uuid-1"})
    assert response.status_code == 200
    assert response.json()["origin"] == {"kind": "project", "project": "My Robot"}
    assert seen["path"] == "src.mine:Mine" and Path(seen["project_dir"]) == project

    response = client.get("/api/v1/camera/backends/describe", params={"path": "raccoon_cam.backends.usb:UsbCameraBackend"})
    assert response.json()["origin"] == {"kind": "builtin", "project": None}
    assert seen["project_dir"] is None


def test_an_unknown_project_is_not_found(server):
    client, _ = server
    response = client.get("/api/v1/camera/backends/describe", params={"path": "a:B", "project_id": "nobody"})
    assert response.status_code == 404


def test_a_class_that_cannot_be_described_is_a_bad_request_with_the_reason(server, monkeypatch):
    client, _ = server

    def describe(*args, **kwargs):
        raise camera_discovery.CameraDiscoveryError("ValueError: cannot load 'a:B'")

    monkeypatch.setattr(camera_discovery, "describe_class", describe)
    response = client.get("/api/v1/camera/backends/describe", params={"path": "a:B"})
    assert response.status_code == 400 and "cannot load 'a:B'" in response.json()["detail"]


def test_without_raccoon_cam_the_routes_say_so(server, monkeypatch):
    client, _ = server
    monkeypatch.setattr(camera_discovery.importlib.util, "find_spec", lambda name: None)
    assert client.get("/api/v1/camera/backends").status_code == 503
    assert client.get("/api/v1/camera/config").status_code == 503


def test_changing_the_choice_needs_the_token(server):
    client, _ = server
    body = {"path": "raccoon_cam.backends.usb:UsbCameraBackend", "cfg": {}}
    assert client.put("/api/v1/camera/config", json=body).status_code in (401, 422)
    assert client.delete("/api/v1/camera/config").status_code in (401, 422)
    assert client.put("/api/v1/camera/config", json=body, headers={"X-API-Token": "wrong"}).status_code == 401


@needs_raccoon_cam
def test_the_choice_is_saved_read_and_cleared_through_the_routes(server):
    client, _ = server
    assert client.get("/api/v1/camera/config").json() == {"backend": None}
    body = {"path": "raccoon_cam.backends.usb:UsbCameraBackend", "cfg": {"fps": "20", "resolution": [640, 480]}}
    response = client.put("/api/v1/camera/config", json=body, headers=auth())
    assert response.status_code == 200 and response.json()["backend"]["cfg"] == {"fps": 20, "resolution": [640, 480]}
    assert client.get("/api/v1/camera/config").json()["backend"]["path"] == body["path"]
    assert client.delete("/api/v1/camera/config", headers=auth()).json() == {"cleared": True}
    assert client.get("/api/v1/camera/config").json() == {"backend": None}


@needs_raccoon_cam
def test_a_configuration_that_does_not_fit_is_refused_and_nothing_is_saved(server):
    client, _ = server
    body = {"path": "raccoon_cam.backends.usb:UsbCameraBackend", "cfg": {"fps": 0}}
    response = client.put("/api/v1/camera/config", json=body, headers=auth())
    assert response.status_code == 400 and "fps must be at least 1" in response.json()["detail"]
    assert client.get("/api/v1/camera/config").json() == {"backend": None}


@needs_raccoon_cam
def test_a_project_backend_is_listed_described_and_chosen_end_to_end(server, project):
    client, _ = server
    listed = client.get("/api/v1/camera/backends").json()["backends"]
    assert [b["name"] for b in listed] == ["usb", "replay", "mine"]
    described = client.get("/api/v1/camera/backends/describe", params={"path": "src.mine:Mine", "project_id": "uuid-1"}).json()
    assert described["options"][0]["name"] == "gain" and described["origin"]["project"] == "My Robot"
    body = {"path": "src.mine:Mine", "cfg": {"gain": "2"}, "project_id": "uuid-1"}
    saved = client.put("/api/v1/camera/config", json=body, headers=auth()).json()["backend"]
    assert saved["cfg"] == {"gain": 2.0} and Path(saved["project_dir"]) == project
