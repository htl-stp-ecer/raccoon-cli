"""Establish the SSH session to a Pi and read its API key.

Order of events (see ``open_ssh_session``):

1. Log in with a public key (SSH agent / ``~/.ssh`` keys).
2. If that is rejected: log in once with the password, install our public key
   (generated if no key exists), then log in again with that key.

Every failure is raised as ``SSHConnectError`` with a message fit for the user;
nothing is swallowed.
"""

import socket
from typing import Callable, Optional

import paramiko

from raccoon_cli.client.ssh_keys import SSHKeyError, SSHKeyManager

API_TOKEN_FILE = ".raccoon/api_token"  # relative to the Pi user's home
CONNECT_TIMEOUT = 10.0


class SSHConnectError(Exception):
    """The SSH session could not be established."""


class ApiTokenError(Exception):
    """The API key could not be read from the target."""


def describe_error(exc: BaseException) -> str:
    """Turn a connection exception into one readable line."""
    if isinstance(exc, socket.gaierror):
        return f"cannot resolve host ({exc.strerror or exc})"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "connection timed out"
    if isinstance(exc, paramiko.BadHostKeyException):
        return f"host key mismatch ({exc})"
    return str(exc) or type(exc).__name__


def _is_auth_failure(exc: BaseException) -> bool:
    """True if the server answered but refused our credentials (or we had none)."""
    if isinstance(exc, paramiko.AuthenticationException):
        return True
    # paramiko raises this when there is neither an agent nor a usable key file
    return isinstance(exc, paramiko.SSHException) and "No authentication methods available" in str(exc)


def _new_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


def open_ssh_session(
    address: str,
    user: str,
    ask_password: Callable[[], Optional[str]],
    log: Callable[[str], None] = lambda _msg: None,
    timeout: float = CONNECT_TIMEOUT,
) -> paramiko.SSHClient:
    """
    Open an authenticated SSH session, installing our public key if needed.

    Args:
        address: Hostname or IP of the target
        user: SSH username
        ask_password: Called only if public key login fails; returns the password
        log: Receives progress messages for the fallback path
        timeout: TCP connect timeout in seconds

    Returns:
        A connected client authenticated by public key. The caller closes it.

    Raises:
        SSHConnectError: with the reason, at whichever step failed
    """
    # 1. Public key (agent + default keys). Only an auth refusal falls through:
    #    an unreachable host or a timeout would fail the password login as well.
    client = _new_client()
    try:
        client.connect(hostname=address, username=user, timeout=timeout)
        return client
    except Exception as exc:
        client.close()
        if not _is_auth_failure(exc):
            raise SSHConnectError(describe_error(exc)) from exc

    log("Public key authentication failed.")

    # 2. Password login + key deposit
    keys = SSHKeyManager()
    try:
        key_path, key, generated = keys.ensure_key()
    except (SSHKeyError, OSError) as exc:
        raise SSHConnectError(f"no usable SSH key: {exc}") from exc
    log(f"{'Generated new' if generated else 'Using existing'} SSH key: {key_path}")

    password = ask_password()
    if not password:
        raise SSHConnectError("no password entered")

    pw_client = _new_client()
    try:
        try:
            pw_client.connect(
                hostname=address,
                username=user,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=timeout,
            )
        except paramiko.BadAuthenticationType as exc:
            raise SSHConnectError(
                f"the target does not allow password login (allowed: {', '.join(exc.allowed_types)})"
            ) from exc
        except Exception as exc:
            if _is_auth_failure(exc):
                raise SSHConnectError("password rejected (wrong password?)") from exc
            raise SSHConnectError(describe_error(exc)) from exc

        try:
            keys.deposit_public_key(pw_client, key)
        except Exception as exc:
            raise SSHConnectError(f"could not install the public key on the target: {describe_error(exc)}") from exc
    finally:
        pw_client.close()
    log("Public key installed on the target.")

    # 3. Prove it worked; this is the session we hand back.
    client = _new_client()
    try:
        client.connect(
            hostname=address,
            username=user,
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
    except Exception as exc:
        client.close()
        raise SSHConnectError(
            f"public key was installed but login with it still fails: {describe_error(exc)}"
        ) from exc
    return client


def read_api_token(client: paramiko.SSHClient) -> str:
    """
    Read the raccoon-server API key from ``~/.raccoon/api_token`` on the target.

    Raises:
        ApiTokenError: The file is missing, unreadable or empty
    """
    try:
        _, stdout, stderr = client.exec_command(f"cat ~/{API_TOKEN_FILE}")
        token = stdout.read().decode(errors="replace").strip()
        status = stdout.channel.recv_exit_status()
        error = stderr.read().decode(errors="replace").strip()
    except Exception as exc:
        raise ApiTokenError(describe_error(exc)) from exc

    if status != 0:
        raise ApiTokenError(error or f"cat exited with status {status}")
    if not token:
        raise ApiTokenError(f"~/{API_TOKEN_FILE} is empty (is raccoon-server set up on the target?)")
    return token
