from __future__ import annotations

import socket
from unittest.mock import MagicMock

import paramiko
import pytest

from raccoon_cli.client import ssh_connect as sc
from raccoon_cli.client.ssh_keys import SSHKeyError


class FakeKeys:
    """Stands in for SSHKeyManager; no files touched."""

    def __init__(self, generated=False, ensure_error=None, deposit_error=None):
        self.key = MagicMock()
        self.generated = generated
        self.ensure_error = ensure_error
        self.deposit_error = deposit_error
        self.deposited = []

    def ensure_key(self):
        if self.ensure_error:
            raise self.ensure_error
        return "/home/x/.ssh/id_ed25519", self.key, self.generated

    def deposit_public_key(self, client, key):
        if self.deposit_error:
            raise self.deposit_error
        self.deposited.append((client, key))


def _patch(monkeypatch, connect_effects, keys=None):
    """connect_effects: one entry per SSHClient.connect call (None = success, else exception)."""
    clients = []
    effects = list(connect_effects)

    def make_client():
        c = MagicMock()
        effect = effects.pop(0)
        c.connect.side_effect = effect
        clients.append(c)
        return c

    monkeypatch.setattr(paramiko, "SSHClient", make_client)
    keys = keys or FakeKeys()
    monkeypatch.setattr(sc, "SSHKeyManager", lambda: keys)
    return clients, keys


def _open(ask_password=lambda: "pw", logs=None):
    logs = logs if logs is not None else []
    return sc.open_ssh_session("pi.local", "pi", ask_password, logs.append), logs


def test_public_key_success_never_asks_for_password(monkeypatch):
    clients, keys = _patch(monkeypatch, [None])
    asked = []

    session, _ = _open(ask_password=lambda: asked.append(1) or "pw")

    assert session is clients[0]
    assert asked == []
    assert keys.deposited == []


def test_auth_failure_falls_back_to_password_and_deposits_key(monkeypatch):
    clients, keys = _patch(monkeypatch, [paramiko.AuthenticationException("no"), None, None])

    session, logs = _open()

    assert session is clients[2]
    assert clients[1].connect.call_args.kwargs["password"] == "pw"
    assert clients[2].connect.call_args.kwargs["pkey"] is keys.key
    assert keys.deposited == [(clients[1], keys.key)]
    clients[1].close.assert_called_once()
    assert "Public key authentication failed." in logs
    assert any("Using existing" in m for m in logs)


def test_no_agent_and_no_keys_also_counts_as_auth_failure(monkeypatch):
    clients, keys = _patch(
        monkeypatch,
        [paramiko.SSHException("No authentication methods available"), None, None],
        keys=FakeKeys(generated=True),
    )

    session, logs = _open()

    assert session is clients[2]
    assert any("Generated new" in m for m in logs)


@pytest.mark.parametrize(
    "exc,text",
    [
        (socket.gaierror(-2, "Name or service not known"), "cannot resolve host"),
        (socket.timeout(), "timed out"),
        (OSError("No route to host"), "No route to host"),
    ],
)
def test_network_errors_stop_without_password_prompt(monkeypatch, exc, text):
    _patch(monkeypatch, [exc])
    asked = []

    with pytest.raises(sc.SSHConnectError, match=text):
        _open(ask_password=lambda: asked.append(1) or "pw")

    assert asked == []


def test_wrong_password_fails(monkeypatch):
    _patch(monkeypatch, [paramiko.AuthenticationException("no"), paramiko.AuthenticationException("no")])

    with pytest.raises(sc.SSHConnectError, match="password rejected"):
        _open()


def test_password_login_disabled_on_target(monkeypatch):
    _patch(
        monkeypatch,
        [paramiko.AuthenticationException("no"), paramiko.BadAuthenticationType("bad", ["publickey"])],
    )

    with pytest.raises(sc.SSHConnectError, match="does not allow password login.*publickey"):
        _open()


def test_empty_password_aborts(monkeypatch):
    _patch(monkeypatch, [paramiko.AuthenticationException("no")])

    with pytest.raises(sc.SSHConnectError, match="no password"):
        _open(ask_password=lambda: "")


def test_unloadable_key_aborts_before_asking_password(monkeypatch):
    _patch(
        monkeypatch,
        [paramiko.AuthenticationException("no")],
        keys=FakeKeys(ensure_error=SSHKeyError("id_ed25519 exists but cannot be loaded")),
    )
    asked = []

    with pytest.raises(sc.SSHConnectError, match="no usable SSH key"):
        _open(ask_password=lambda: asked.append(1) or "pw")

    assert asked == []


def test_deposit_failure_is_reported_and_password_session_closed(monkeypatch):
    clients, _ = _patch(
        monkeypatch,
        [paramiko.AuthenticationException("no"), None],
        keys=FakeKeys(deposit_error=SSHKeyError("Read-only file system")),
    )

    with pytest.raises(sc.SSHConnectError, match="could not install.*Read-only"):
        _open()

    clients[1].close.assert_called_once()


def test_key_installed_but_login_still_fails(monkeypatch):
    _patch(
        monkeypatch,
        [paramiko.AuthenticationException("no"), None, paramiko.AuthenticationException("no")],
    )

    with pytest.raises(sc.SSHConnectError, match="still fails"):
        _open()


def _client_with_cat(out=b"", err=b"", status=0):
    c = MagicMock()
    stdout, stderr = MagicMock(), MagicMock()
    stdout.read.return_value = out
    stdout.channel.recv_exit_status.return_value = status
    stderr.read.return_value = err
    c.exec_command.return_value = (MagicMock(), stdout, stderr)
    return c


def test_read_api_token_ok():
    assert sc.read_api_token(_client_with_cat(out=b"tok\n")) == "tok"


def test_read_api_token_missing_file_reports_stderr():
    c = _client_with_cat(err=b"cat: /home/pi/.raccoon/api_token: No such file or directory", status=1)
    with pytest.raises(sc.ApiTokenError, match="No such file"):
        sc.read_api_token(c)


def test_read_api_token_empty_file():
    with pytest.raises(sc.ApiTokenError, match="empty"):
        sc.read_api_token(_client_with_cat(out=b"\n"))


def test_read_api_token_exec_failure():
    c = MagicMock()
    c.exec_command.side_effect = paramiko.SSHException("channel closed")
    with pytest.raises(sc.ApiTokenError, match="channel closed"):
        sc.read_api_token(c)
