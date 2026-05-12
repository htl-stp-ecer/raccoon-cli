from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raccoon_cli.server import app as app_module
from raccoon_cli.server.app import create_app
from raccoon_cli.server.config import ServerConfig


@pytest.fixture
def server_client(tmp_path: Path, monkeypatch):
    config = ServerConfig(projects_dir=tmp_path / "programs", api_token="test-token")
    config.projects_dir.mkdir(parents=True, exist_ok=True)
    app_module._config = config

    from raccoon_cli.server import auth as auth_module

    monkeypatch.setattr(auth_module, "get_or_create_api_token", lambda: "test-token")

    app = create_app(config=config)
    with TestClient(app) as client:
        yield client

    app_module._config = None


def _auth() -> dict[str, str]:
    return {"X-API-Token": "test-token"}


def test_networks_route_returns_scanned_networks(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    monkeypatch.setattr(
        network_route._network_manager,
        "scan_networks",
        lambda: [
            {
                "ssid": "RobotNet",
                "encryptionType": "wpa2Personal",
                "isKnown": True,
                "isConnected": False,
            }
        ],
    )

    response = server_client.get("/api/v1/network/networks", headers=_auth())
    assert response.status_code == 200
    assert response.json()[0]["ssid"] == "RobotNet"


def test_lan_status_route_exposes_connected_ethernet(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    monkeypatch.setattr(
        network_route._network_manager,
        "lan_status",
        lambda: {
            "isActive": True,
            "isCableConnected": True,
            "ipAddress": "192.168.0.22",
            "macAddress": "AA:BB:CC:DD:EE:FF",
        },
    )

    response = server_client.get("/api/v1/network/lan/status", headers=_auth())
    assert response.status_code == 200
    data = response.json()
    assert data["isCableConnected"] is True
    assert data["ipAddress"] == "192.168.0.22"


def test_saved_network_roundtrip(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    saved = []

    def save_network(payload):
        saved.append(payload)

    monkeypatch.setattr(network_route._network_manager, "save_network", save_network)
    monkeypatch.setattr(network_route._network_manager, "get_saved_networks", lambda: saved)

    response = server_client.put(
        "/api/v1/network/saved",
        headers=_auth(),
        json={
            "ssid": "RobotNet",
            "encryptionType": "wpa2Personal",
            "credentials": {
                "credentialsType": "personal",
                "password": "secret123",
            },
            "lastConnected": "2026-05-12T12:00:00.000Z",
            "autoConnect": True,
        },
    )
    assert response.status_code == 200

    follow_up = server_client.get("/api/v1/network/saved", headers=_auth())
    assert follow_up.status_code == 200
    assert follow_up.json()[0]["ssid"] == "RobotNet"


def test_connect_errors_become_http_400(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(network_route._network_manager, "connect", fail)

    response = server_client.post(
        "/api/v1/network/connect",
        headers=_auth(),
        json={
            "ssid": "RobotNet",
            "encryptionType": "wpa2Personal",
            "credentials": {
                "credentialsType": "personal",
                "password": "secret123",
            },
        },
    )
    assert response.status_code == 400
