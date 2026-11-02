from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import paramiko

from raccoon_cli.client import connection as conn


class _FakeHealthResponse:
    status_code = 200

    def json(self):
        return {"version": conn.CLIENT_VERSION, "hostname": "wombat"}


def _setup(monkeypatch):
    clients: list[MagicMock] = []

    def make_client():
        c = MagicMock()
        stdout = MagicMock()
        stdout.read.return_value = b"tok123\n"
        c.exec_command.return_value = (MagicMock(), stdout, MagicMock())
        clients.append(c)
        return c

    monkeypatch.setattr(paramiko, "SSHClient", make_client)
    monkeypatch.setattr(conn, "check_paramiko_version", lambda: None)

    async def fake_get(self, url, **kw):
        return _FakeHealthResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return clients


def test_connect_then_get_ssh_client_uses_one_login(monkeypatch):
    clients = _setup(monkeypatch)
    manager = conn.ConnectionManager()

    assert manager.connect_sync("192.0.2.1", 8421, "pi")
    ssh = manager.get_ssh_client()

    assert len(clients) == 1
    assert ssh is clients[0]
    assert clients[0].connect.call_count == 1
    clients[0].close.assert_not_called()
    assert manager.state.api_token == "tok123"

    manager.disconnect()
    clients[0].close.assert_called_once()


def test_standalone_token_fetch_closes_client(monkeypatch):
    clients = _setup(monkeypatch)
    manager = conn.ConnectionManager()

    assert manager._fetch_api_token_via_ssh("192.0.2.1", "pi") == "tok123"

    clients[0].close.assert_called_once()
    assert manager._ssh_client is None
