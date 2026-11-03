from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner
from rich.console import Console

from raccoon_cli.commands import connect as cmd
from raccoon_cli.client import connection as conn
from raccoon_cli.client.discovery import DiscoveredPi


def _run(monkeypatch, *, open_session, token=lambda c: "tok", info=True):
    manager = conn.ConnectionManager()
    saved = []
    monkeypatch.setattr(manager, "save_to_global", lambda: saved.append("global"))
    monkeypatch.setattr(cmd, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(cmd, "check_paramiko_version", lambda: None)
    monkeypatch.setattr(cmd, "find_project_root", lambda: None)
    monkeypatch.setattr(cmd, "open_ssh_session", open_session)
    monkeypatch.setattr(cmd, "read_api_token", token)
    monkeypatch.setattr(cmd, "_warn_about_contract", lambda *a: None)

    async def fake_check(address, port, **kw):
        return DiscoveredPi(address=address, port=port, hostname="wombat", version=conn.CLIENT_VERSION) if info else None

    monkeypatch.setattr(cmd, "check_address", fake_check)

    console = Console(width=200, record=True, force_terminal=False)
    result = CliRunner().invoke(cmd.connect_command, ["10.0.0.5"], obj={"console": console})
    return result, console.export_text(), manager, saved


def test_success_prints_connected_then_retrieves_key_and_saves(monkeypatch):
    ssh = MagicMock()
    result, out, manager, saved = _run(monkeypatch, open_session=lambda *a, **k: ssh)

    assert result.exit_code == 0
    assert out.index("Connected!") < out.index("Retrieving API key") < out.index("API key retrieved")
    assert manager.state.api_token == "tok"
    assert manager.get_ssh_client() is ssh
    assert saved == ["global"]


def test_connect_failure_stops_before_api_key(monkeypatch):
    def boom(*a, **k):
        raise cmd.SSHConnectError("password rejected (wrong password?)")

    token = MagicMock()
    result, out, manager, saved = _run(monkeypatch, open_session=boom, token=token)

    assert result.exit_code == 1
    assert "Failed to connect to pi@10.0.0.5: password rejected" in out
    assert "Connected!" not in out
    token.assert_not_called()
    assert not manager.is_connected
    assert saved == []


def test_api_key_failure_is_logged_and_aborts(monkeypatch):
    ssh = MagicMock()

    def no_token(client):
        raise cmd.ApiTokenError("cat: /home/pi/.raccoon/api_token: No such file or directory")

    result, out, manager, saved = _run(monkeypatch, open_session=lambda *a, **k: ssh, token=no_token)

    assert result.exit_code == 1
    assert "Connected!" in out
    assert "Failed to retrieve the API key" in out and "No such file" in out
    ssh.close.assert_called_once()
    assert not manager.is_connected
    assert saved == []
