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


def test_channel_scan_route_returns_visualization_payload(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    monkeypatch.setattr(
        network_route._network_manager,
        "scan_access_point_channels",
        lambda band: {
            "band": band,
            "recommendedChannel": 6,
            "detectedNetworks": 3,
            "channels": [
                {
                    "channel": 1,
                    "networkCount": 2,
                    "ssids": ["RobotNet", "Pit AP"],
                    "isRecommended": False,
                },
                {
                    "channel": 6,
                    "networkCount": 0,
                    "ssids": [],
                    "isRecommended": True,
                },
            ],
            "networks": [
                {
                    "ssid": "RobotNet",
                    "channel": 1,
                    "frequencyMHz": 2412,
                    "centerFrequencyMHz": 2412,
                    "channelWidthMHz": 20,
                    "signalDbm": -42,
                    "qualityPercent": 92,
                    "overlapStartChannel": 1,
                    "overlapEndChannel": 3,
                    "affectedChannels": [1, 2, 3],
                }
            ],
        },
    )

    response = server_client.get(
        "/api/v1/network/access-point/channel-scan?band=band2_4GHz",
        headers=_auth(),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["recommendedChannel"] == 6
    assert data["channels"][1]["isRecommended"] is True
    assert data["networks"][0]["ssid"] == "RobotNet"


def test_saved_network_roundtrip(server_client, monkeypatch):
    from raccoon_cli.server.routes import network as network_route

    saved = []

    def save_network(payload):
        saved.append(payload)

    monkeypatch.setattr(network_route._network_manager, "save_network", save_network)
    monkeypatch.setattr(
        network_route._network_manager, "get_saved_networks", lambda: saved
    )

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

    monkeypatch.setattr(
        network_route._network_manager,
        "set_network_mode",
        lambda mode: calls.append(mode),
    )

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
            "ssid": "RaccoonOS",
            "password": "raccoonos",
            "band": "band5GHz",
            "channel": 0,
            "encryptionType": "wpa2Personal",
            "hidden": False,
            "maxClients": 8,
        }
    ]


def test_network_manager_restore_restarts_saved_hotspot_config(
    tmp_path: Path, monkeypatch
):
    state_path = tmp_path / "network_state.json"
    state_path.write_text("""
{
  "network_mode": "access_point",
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
""".strip())
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


def test_network_manager_channel_scan_summarizes_channels(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")

    monkeypatch.setattr(manager, "_get_wifi_interface", lambda: "wlan0")
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, check=True: CompletedProcess(
            command,
            0,
            stdout="""
              Cell 01 - Address: AA:BB:CC:DD:EE:01
                        Channel:1
                        Frequency:2.412 GHz
                        Quality=64/70  Signal level=-46 dBm
                        ESSID:"RobotNet"
              Cell 02 - Address: AA:BB:CC:DD:EE:02
                        Frequency:2.437 GHz (Channel 6)
                        HT40
                        secondary channel offset: above
                        Quality=43/70  Signal level=-68 dBm
                        ESSID:"Practice Field"
              Cell 03 - Address: AA:BB:CC:DD:EE:03
                        Channel:1
                        ESSID:""
            """,
            stderr="",
        ),
    )

    scan = manager.scan_access_point_channels("band2_4GHz")

    assert scan["band"] == "band2_4GHz"
    assert scan["recommendedChannel"] == 6
    assert scan["detectedNetworks"] == 3
    assert len(scan["channels"]) == 13
    assert scan["channels"][0] == {
        "channel": 1,
        "networkCount": 2,
        "ssids": ["RobotNet", "<Hidden>"],
        "isRecommended": False,
    }
    assert scan["channels"][5] == {
        "channel": 6,
        "networkCount": 1,
        "ssids": ["Practice Field"],
        "isRecommended": True,
    }
    assert scan["channels"][10] == {
        "channel": 11,
        "networkCount": 1,
        "ssids": ["Practice Field"],
        "isRecommended": False,
    }
    assert scan["networks"][0] == {
        "ssid": "RobotNet",
        "channel": 1,
        "frequencyMHz": 2412,
        "centerFrequencyMHz": 2412,
        "channelWidthMHz": 20,
        "signalDbm": -46,
        "qualityPercent": 91,
        "overlapStartChannel": 1,
        "overlapEndChannel": 5,
        "affectedChannels": [1, 2, 3, 4, 5],
    }
    assert scan["networks"][1] == {
        "ssid": "<Hidden>",
        "channel": 1,
        "frequencyMHz": 2412,
        "centerFrequencyMHz": 2412,
        "channelWidthMHz": 20,
        "signalDbm": None,
        "qualityPercent": None,
        "overlapStartChannel": 1,
        "overlapEndChannel": 5,
        "affectedChannels": [1, 2, 3, 4, 5],
    }
    assert scan["networks"][2] == {
        "ssid": "Practice Field",
        "channel": 6,
        "frequencyMHz": 2437,
        "centerFrequencyMHz": 2447,
        "channelWidthMHz": 40,
        "signalDbm": -68,
        "qualityPercent": 61,
        "overlapStartChannel": 2,
        "overlapEndChannel": 13,
        "affectedChannels": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
    }


def test_network_manager_channel_scan_temporarily_restarts_hotspot(
    tmp_path: Path, monkeypatch
):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    config = {
        "ssid": "RobotNet",
        "password": "secret123",
        "band": "band2_4GHz",
        "channel": 6,
        "encryptionType": "wpa2Personal",
        "hidden": False,
        "maxClients": 8,
    }

    calls: list[object] = []
    state = {"active": True}

    monkeypatch.setattr(manager, "get_access_point_config", lambda: config)
    monkeypatch.setattr(manager, "find_best_wifi_band", lambda: "band2_4GHz")
    monkeypatch.setattr(manager, "_get_wifi_interface", lambda: "wlan0")

    def fake_is_active():
        return state["active"]

    def fake_stop():
        calls.append("stop")
        state["active"] = False

    def fake_start(restart_config):
        calls.append(("start", restart_config))
        state["active"] = True
        return restart_config

    monkeypatch.setattr(manager, "is_access_point_active", fake_is_active)
    monkeypatch.setattr(manager, "stop_access_point", fake_stop)
    monkeypatch.setattr(manager, "start_access_point", fake_start)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, check=True: CompletedProcess(
            command,
            0,
            stdout="""
              Cell 01 - Address: AA:BB:CC:DD:EE:01
                        Channel:6
                        Frequency:2.437 GHz
                        ESSID:"Practice Field"
            """,
            stderr="",
        ),
    )

    scan = manager.scan_access_point_channels("band2_4GHz")

    assert calls == ["stop", ("start", config)]
    assert scan["detectedNetworks"] == 1
    assert state["active"] is True


def test_network_manager_detects_active_hotspot_from_connection_show(
    tmp_path: Path, monkeypatch
):
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
            return CompletedProcess(
                command, 0, stdout="RobotNet:802-11-wireless:wlan0\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(manager, "_run", fake_run)

    assert manager.is_access_point_active() is True


def test_network_manager_detects_active_hotspot_from_device_status_fallback(
    tmp_path: Path, monkeypatch
):
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
            return CompletedProcess(
                command, 0, stdout="SomeOther:wifi:wlan0\n", stderr=""
            )
        if command[:4] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            return CompletedProcess(
                command, 0, stdout="wlan0:wifi:connected:RobotNet\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(manager, "_run", fake_run)

    assert manager.is_access_point_active() is True


def test_network_manager_stop_access_point_stops_saved_ssid_connection(
    tmp_path: Path, monkeypatch
):
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

    monkeypatch.setattr(
        manager, "_reset_wifi_interface", lambda: commands.append(["reset"])
    )

    def fake_run(command, check=True):
        commands.append(command)
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", fake_run)

    manager.stop_access_point()

    assert commands == [
        ["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"],
        ["nmcli", "connection", "down", "RobotNet"],
        ["nmcli", "connection", "delete", "RobotNet"],
        ["nmcli", "connection", "down", "STP-Velox-AP"],
        ["nmcli", "connection", "delete", "STP-Velox-AP"],
        ["reset"],
    ]


def _hotspot_config(**overrides):
    return {
        "ssid": "RobotNet",
        "password": "secret123",
        "band": "band2_4GHz",
        "channel": 6,
        "encryptionType": "wpa2Personal",
        "hidden": False,
        "maxClients": 8,
        **overrides,
    }


def _hotspot_manager(tmp_path: Path, monkeypatch, commands, fail_sae=False):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    monkeypatch.setattr(manager, "stop_access_point", lambda: None)
    monkeypatch.setattr(
        manager, "_ensure_wifi_enabled", lambda: commands.append(["ensure"])
    )
    monkeypatch.setattr(manager, "_get_wifi_interface", lambda: "wlan0")

    def fake_run(command, check=True):
        commands.append(command)
        if fail_sae and "sae" in command and "add" in command:
            raise RuntimeError("sae unsupported")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", fake_run)
    return manager


def _add_command(commands):
    return next(c for c in commands if c[:3] == ["nmcli", "connection", "add"])


def test_start_access_point_creates_wpa2_profile_with_pmf_disabled(
    tmp_path: Path, monkeypatch
):
    commands: list = []
    manager = _hotspot_manager(tmp_path, monkeypatch, commands)

    applied = manager.start_access_point(_hotspot_config(hidden=True))

    assert commands[0] == ["ensure"]
    add = _add_command(commands)
    assert "hotspot" not in add
    assert add[add.index("wifi-sec.pmf") + 1] == "disable"
    assert add[add.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
    assert add[add.index("802-11-wireless.hidden") + 1] == "yes"
    assert add[add.index("802-11-wireless.channel") + 1] == "6"
    assert commands[-1] == ["nmcli", "connection", "up", "STP-Velox-AP"]
    assert applied["encryptionType"] == "wpa2Personal"


def test_start_access_point_wpa3_requires_pmf(tmp_path: Path, monkeypatch):
    commands: list = []
    manager = _hotspot_manager(tmp_path, monkeypatch, commands)

    applied = manager.start_access_point(_hotspot_config(encryptionType="wpa3Personal"))

    add = _add_command(commands)
    assert add[add.index("wifi-sec.key-mgmt") + 1] == "sae"
    assert add[add.index("wifi-sec.pmf") + 1] == "required"
    assert applied["encryptionType"] == "wpa3Personal"


def test_start_access_point_falls_back_to_wpa2_when_sae_fails(
    tmp_path: Path, monkeypatch
):
    commands: list = []
    manager = _hotspot_manager(tmp_path, monkeypatch, commands, fail_sae=True)

    applied = manager.start_access_point(_hotspot_config(encryptionType="wpa3Personal"))

    assert applied["encryptionType"] == "wpa2Personal"
    adds = [c for c in commands if c[:3] == ["nmcli", "connection", "add"]]
    assert adds[0][adds[0].index("wifi-sec.key-mgmt") + 1] == "sae"
    assert adds[1][adds[1].index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
    assert ["nmcli", "connection", "delete", "STP-Velox-AP"] in commands


def test_start_access_point_open_has_no_security(tmp_path: Path, monkeypatch):
    commands: list = []
    manager = _hotspot_manager(tmp_path, monkeypatch, commands)

    applied = manager.start_access_point(
        _hotspot_config(encryptionType="open", password="")
    )

    assert not any(arg.startswith("wifi-sec") for arg in _add_command(commands))
    assert applied["encryptionType"] == "open"


def test_stop_access_point_keeps_client_profile_with_same_name(
    tmp_path: Path, monkeypatch
):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    manager.save_access_point_config(_hotspot_config())
    monkeypatch.setattr(manager, "_reset_wifi_interface", lambda: None)
    monkeypatch.setattr(
        manager,
        "_wifi_profiles",
        lambda: [
            {"uuid": "u1", "name": "RobotNet", "ssid": "RobotNet", "mode": "infrastructure"}
        ],
    )
    commands = []
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, check=True: commands.append(command)
        or CompletedProcess(command, 0, stdout="", stderr=""),
    )

    manager.stop_access_point()

    assert ["nmcli", "connection", "delete", "RobotNet"] not in commands
    assert ["nmcli", "connection", "delete", "STP-Velox-AP"] in commands


def test_forget_never_deletes_hotspot_profile(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    monkeypatch.setattr(
        manager,
        "_wifi_profiles",
        lambda: [
            {"uuid": "ap", "name": "STP-Velox-AP", "ssid": "RobotNet", "mode": "ap"},
            {"uuid": "c1", "name": "RobotNet", "ssid": "RobotNet", "mode": "infrastructure"},
        ],
    )
    commands = []
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, check=True: commands.append(command)
        or CompletedProcess(command, 0, stdout="", stderr=""),
    )

    manager.forget("RobotNet")

    assert commands == [["nmcli", "connection", "delete", "uuid", "c1"]]


def test_lan_only_failsafe_reenables_wifi_after_cable_loss(
    tmp_path: Path, monkeypatch
):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    manager._persist_network_mode("lan_only")
    monkeypatch.setattr(manager, "is_ethernet_cable_connected", lambda: False)
    monkeypatch.setattr(manager, "is_access_point_active", lambda: False)
    monkeypatch.setattr(manager, "_ensure_wifi_enabled", lambda: None)

    assert manager.lan_only_failsafe_tick() is False
    assert manager.lan_only_failsafe_tick() is False
    assert manager.lan_only_failsafe_tick() is True
    assert manager.get_network_mode() == "client"


def test_lan_only_failsafe_ignores_connected_cable(tmp_path: Path, monkeypatch):
    manager = NetworkManager(state_path=tmp_path / "network_state.json")
    manager._persist_network_mode("lan_only")
    monkeypatch.setattr(manager, "is_ethernet_cable_connected", lambda: True)

    for _ in range(5):
        assert manager.lan_only_failsafe_tick() is False
    assert manager.get_network_mode() == "lan_only"
