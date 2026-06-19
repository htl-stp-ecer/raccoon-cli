"""Tests that sync checkpoints run from shared sync paths.

Pre-sync checkpoints use the invisible ``git stash create`` mechanism in
:mod:`raccoon_cli.checkpoint`; they must never produce real commits.
"""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from raccoon_cli.checkpoint import CheckpointResult
from raccoon_cli.client.sftp_sync import SyncDirection, SyncResult
from raccoon_cli.commands import sync_cmd


def test_do_sync_creates_checkpoint_for_push(monkeypatch):
    project_root = Path("/tmp/demo-project")
    checkpoint_calls = []
    rsync_calls = []

    manager = SimpleNamespace(
        is_connected=True,
        state=SimpleNamespace(
            pi_user="pi",
            pi_address="192.168.4.1",
            pi_hostname="wombat",
            api_token=None,
        ),
    )

    class FakeRsync:
        def __init__(self, host: str, user: str):
            self.host = host
            self.user = user

        def sync(self, local_path: Path, remote_path: str, options):
            rsync_calls.append((local_path, remote_path, options.direction))
            return SyncResult(success=True)

    def fake_checkpoint(project_root: Path, label: str = "checkpoint"):
        checkpoint_calls.append((project_root, label))
        return CheckpointResult(created=False, reason="no_changes")

    monkeypatch.setattr(sync_cmd, "load_project_config", lambda _: {"uuid": "proj-123", "name": "Demo"})
    monkeypatch.setattr(sync_cmd, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(sync_cmd, "create_sync", lambda host, user: FakeRsync(host=host, user=user))
    monkeypatch.setattr(sync_cmd, "load_raccoonignore", lambda _: [])
    monkeypatch.setattr(sync_cmd, "create_checkpoint", fake_checkpoint)

    ok = sync_cmd.do_sync(project_root, Console(), direction=SyncDirection.PUSH)

    assert ok is True
    assert checkpoint_calls == [
        (project_root, "pre-push-sync"),
    ]
    assert rsync_calls == [
        (project_root, "/home/pi/programs/proj-123", SyncDirection.PUSH),
    ]


def test_do_sync_creates_checkpoint_for_pull(monkeypatch):
    project_root = Path("/tmp/demo-project")
    checkpoint_calls = []

    manager = SimpleNamespace(
        is_connected=True,
        state=SimpleNamespace(
            pi_user="pi",
            pi_address="192.168.4.1",
            pi_hostname="wombat",
            api_token=None,
        ),
    )

    class FakeRsync:
        def __init__(self, host: str, user: str):
            self.host = host
            self.user = user

        def sync(self, local_path: Path, remote_path: str, options):
            return SyncResult(success=True)

    def fake_checkpoint(project_root: Path, label: str = "checkpoint"):
        checkpoint_calls.append((project_root, label))
        return CheckpointResult(created=False, reason="no_changes")

    monkeypatch.setattr(sync_cmd, "load_project_config", lambda _: {"uuid": "proj-123", "name": "Demo"})
    monkeypatch.setattr(sync_cmd, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(sync_cmd, "create_sync", lambda host, user: FakeRsync(host=host, user=user))
    monkeypatch.setattr(sync_cmd, "load_raccoonignore", lambda _: [])
    monkeypatch.setattr(sync_cmd, "create_checkpoint", fake_checkpoint)

    ok = sync_cmd.do_sync(project_root, Console(), direction=SyncDirection.PULL)

    assert ok is True
    assert checkpoint_calls == [
        (project_root, "pre-pull-sync"),
    ]


def test_do_sync_prints_short_sha_when_checkpoint_created(monkeypatch):
    """Regression: CheckpointResult.short_sha must be surfaced to the user."""
    project_root = Path("/tmp/demo-project")

    manager = SimpleNamespace(
        is_connected=True,
        state=SimpleNamespace(
            pi_user="pi",
            pi_address="192.168.4.1",
            pi_hostname="wombat",
            api_token=None,
        ),
    )

    class FakeRsync:
        def __init__(self, host: str, user: str):
            pass

        def sync(self, local_path: Path, remote_path: str, options):
            return SyncResult(success=True)

    def fake_checkpoint(project_root: Path, label: str = "checkpoint"):
        return CheckpointResult(created=True, sha="abc1234def5678", short_sha="abc1234")

    buf = StringIO()
    console = Console(file=buf, highlight=False)

    monkeypatch.setattr(sync_cmd, "load_project_config", lambda _: {"uuid": "proj-123", "name": "Demo"})
    monkeypatch.setattr(sync_cmd, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(sync_cmd, "create_sync", lambda host, user: FakeRsync(host=host, user=user))
    monkeypatch.setattr(sync_cmd, "load_raccoonignore", lambda _: [])
    monkeypatch.setattr(sync_cmd, "create_checkpoint", fake_checkpoint)

    ok = sync_cmd.do_sync(project_root, console, direction=SyncDirection.PUSH)

    assert ok is True
    output = buf.getvalue()
    assert "abc1234" in output
