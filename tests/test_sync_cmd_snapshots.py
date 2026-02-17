"""Tests that sync snapshots run from shared sync paths."""

from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from raccoon.client.sftp_sync import SyncDirection, SyncResult
from raccoon.commands import sync_cmd
from raccoon.git_history import GitSnapshotResult


def test_do_sync_creates_snapshot_for_push(monkeypatch):
    project_root = Path("/tmp/demo-project")
    snapshot_calls = []
    rsync_calls = []

    manager = SimpleNamespace(
        is_connected=True,
        state=SimpleNamespace(
            pi_user="pi",
            pi_address="192.168.4.1",
            pi_hostname="wombat",
        ),
    )

    class FakeRsync:
        def __init__(self, host: str, user: str):
            self.host = host
            self.user = user

        def sync(self, local_path: Path, remote_path: str, options):
            rsync_calls.append((local_path, remote_path, options.direction))
            return SyncResult(success=True)

    def fake_snapshot(project_root: Path, direction: str, target: str):
        snapshot_calls.append((project_root, direction, target))
        return GitSnapshotResult(created=False, reason="no_changes")

    monkeypatch.setattr(sync_cmd, "load_project_config", lambda _: {"uuid": "proj-123", "name": "Demo"})
    monkeypatch.setattr(sync_cmd, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(sync_cmd, "create_sync", lambda host, user: FakeRsync(host=host, user=user))
    monkeypatch.setattr(sync_cmd, "load_raccoonignore", lambda _: [])
    monkeypatch.setattr(sync_cmd, "create_pre_sync_snapshot", fake_snapshot)

    ok = sync_cmd.do_sync(project_root, Console(), direction=SyncDirection.PUSH)

    assert ok is True
    assert snapshot_calls == [
        (project_root, "push", "pi@192.168.4.1:/home/pi/programs/proj-123"),
    ]
    assert rsync_calls == [
        (project_root, "/home/pi/programs/proj-123", SyncDirection.PUSH),
    ]


def test_do_sync_creates_snapshot_for_pull(monkeypatch):
    project_root = Path("/tmp/demo-project")
    snapshot_calls = []

    manager = SimpleNamespace(
        is_connected=True,
        state=SimpleNamespace(
            pi_user="pi",
            pi_address="192.168.4.1",
            pi_hostname="wombat",
        ),
    )

    class FakeRsync:
        def __init__(self, host: str, user: str):
            self.host = host
            self.user = user

        def sync(self, local_path: Path, remote_path: str, options):
            return SyncResult(success=True)

    def fake_snapshot(project_root: Path, direction: str, target: str):
        snapshot_calls.append((project_root, direction, target))
        return GitSnapshotResult(created=False, reason="no_changes")

    monkeypatch.setattr(sync_cmd, "load_project_config", lambda _: {"uuid": "proj-123", "name": "Demo"})
    monkeypatch.setattr(sync_cmd, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(sync_cmd, "create_sync", lambda host, user: FakeRsync(host=host, user=user))
    monkeypatch.setattr(sync_cmd, "load_raccoonignore", lambda _: [])
    monkeypatch.setattr(sync_cmd, "create_pre_sync_snapshot", fake_snapshot)

    ok = sync_cmd.do_sync(project_root, Console(), direction=SyncDirection.PULL)

    assert ok is True
    assert snapshot_calls == [
        (project_root, "pull", "pi@192.168.4.1:/home/pi/programs/proj-123"),
    ]

