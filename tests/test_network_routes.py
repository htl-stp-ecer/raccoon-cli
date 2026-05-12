from pathlib import Path
from subprocess import CompletedProcess

import pytest
from fastapi.testclient import TestClient

from raccoon_cli.server import app as app_module
from raccoon_cli.server.app import create_app
from raccoon_cli.server.config import ServerConfig
from raccoon_cli.server.services.network_manager import NetworkManager


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


def test_set_mode_route_applies_access_point_mode(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    calls = []

    monkeypatch.setattr(network_route._network_manager, "set_network_mode", lambda mode: calls.append(mode))

    response = server_client.put(
        "/api/v1/network/mode",
        headers=_auth(),
        json={"mode": "access_point"},
    )

    assert response.status_code == 200
    assert calls == ["access_point"]


def test_app_startup_restores_persisted_network_mode(tmp_path: Path, monkeypatch):
    from raccoon_cli.server import app as app_module
    from raccoon_cli.server import auth as auth_module

    config = ServerConfig(projects_dir=tmp_path / "programs", api_token="test-token")
    config.projects_dir.mkdir(parents=True, exist_ok=True)
    app_module._config = config

    restored = []

    monkeypatch.setattr(auth_module, "get_or_create_api_token", lambda: "test-token")
    monkeypatch.setattr(
        app_module.NetworkManager,
        "restore_persisted_network_mode",
        lambda self: restored.append(True),
    )

    app = create_app(config=config)
    with TestClient(app):
        pass

    assert restored == [True]
    app_module._config = None


def test_network_manager_access_point_mode_starts_hotspot(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")

    started_configs = []

    monkeypatch.setattr(manager, "find_best_wifi_band", lambda: "band5GHz")
    monkeypatch.setattr(manager, "is_access_point_active", lambda: False)
    monkeypatch.setattr(
        manager,
        "start_access_point",
        lambda config: started_configs.append(config) or config,
    )

    manager.set_network_mode("access_point")

    assert started_configs == [
        {
            "ssid": "STP-Velox-Robot",
            "password": "Robot123!",
            "band": "band5GHz",
            "channel": 0,
            "encryptionType": "wpa3Personal",
            "hidden": False,
            "maxClients": 8,
        }
    ]


def test_network_manager_restore_restarts_saved_hotspot_config(tmp_path: Path, monkeypatch):
    state_path = tmp_path / "network_state.json"
    state_path.write_text(
        """
{
  "network_mode": "access_point",
  "saved_networks": [],
  "access_point_config": {
    "ssid": "RobotNet",
    "password": "secret123",
    "band": "band2_4GHz",
    "channel": 6,
    "encryptionType": "wpa2Personal",
    "hidden": false,
    "maxClients": 8
  }
}
""".strip()
    )
    manager = NetworkManager(state_path=state_path)

    started_configs = []

    monkeypatch.setattr(manager, "is_access_point_active", lambda: False)
    monkeypatch.setattr(
        manager,
        "start_access_point",
        lambda config: started_configs.append(config) or config,
    )

    manager.restore_persisted_network_mode()

    assert started_configs == [
        {
            "ssid": "RobotNet",
            "password": "secret123",
            "band": "band2_4GHz",
            "channel": 6,
            "encryptionType": "wpa2Personal",
            "hidden": False,
            "maxClients": 8,
        }
    ]


def test_network_manager_detects_active_hotspot_from_connection_show(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    manager.save_access_point_config(
        {
            "ssid": "RobotNet",
            "password": "secret123",
            "band": "band2_4GHz",
            "channel": 6,
            "encryptionType": "wpa2Personal",
            "hidden": False,
            "maxClients": 8,
        }
    )

    def fake_run(command, check=True):
        if command[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE"]:
            return CompletedProcess(command, 0, stdout="RobotNet:802-11-wireless:wlan0\n", stderr="")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(manager, "_run", fake_run)

    assert manager.is_access_point_active() is True


def test_network_manager_detects_active_hotspot_from_device_status_fallback(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    manager.save_access_point_config(
        {
            "ssid": "RobotNet",
            "password": "secret123",
            "band": "band2_4GHz",
            "channel": 6,
            "encryptionType": "wpa2Personal",
            "hidden": False,
            "maxClients": 8,
        }
    )

    def fake_run(command, check=True):
        if command[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE"]:
            return CompletedProcess(command, 0, stdout="SomeOther:wifi:wlan0\n", stderr="")
        if command[:4] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            return CompletedProcess(command, 0, stdout="wlan0:wifi:connected:RobotNet\n", stderr="")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(manager, "_run", fake_run)

    assert manager.is_access_point_active() is True


def test_network_manager_stop_access_point_stops_saved_ssid_connection(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    manager.save_access_point_config(
        {
            "ssid": "RobotNet",
            "password": "secret123",
            "band": "band2_4GHz",
            "channel": 6,
            "encryptionType": "wpa2Personal",
            "hidden": False,
            "maxClients": 8,
        }
    )

    commands = []

    monkeypatch.setattr(manager, "_reset_wifi_interface", lambda: commands.append(["reset"]))

    def fake_run(command, check=True):
        commands.append(command)
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", fake_run)

    manager.stop_access_point()

    assert commands == [
        ["nmcli", "connection", "down", "RobotNet"],
        ["nmcli", "connection", "delete", "RobotNet"],
        ["nmcli", "connection", "down", "STP-Velox-AP"],
        ["nmcli", "connection", "delete", "STP-Velox-AP"],
        ["reset"],
    ]
