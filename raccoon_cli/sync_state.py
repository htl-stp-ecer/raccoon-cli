"""Read/write for the ``.raccoon/sync_state.json`` file.

The sync state records the last successful sync: a monotonically increasing
version counter and the content fingerprint that was verified at that moment.
It lives at ``<project_root>/.raccoon/sync_state.json`` on both the local
machine and the Pi, with the same schema on each side.

The file is excluded from sync (``.raccoon`` is in the default ignore list),
so pushing does not overwrite it and ``--delete`` does not remove it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


SYNC_STATE_REL_PATH = ".raccoon/sync_state.json"


@dataclass
class SyncState:
    """Persisted sync state for a project."""

    version: int = 0
    fingerprint: Optional[str] = None
    synced_at: Optional[str] = None
    synced_by: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SyncState":
        return cls(
            version=int(data.get("version", 0)),
            fingerprint=data.get("fingerprint"),
            synced_at=data.get("synced_at"),
            synced_by=data.get("synced_by"),
        )


def sync_state_path(project_root: Path) -> Path:
    return project_root / SYNC_STATE_REL_PATH


def read_sync_state(project_root: Path) -> SyncState:
    """Return the current sync state, or a zero-version default if missing/corrupt."""
    path = sync_state_path(project_root)
    if not path.exists():
        return SyncState()
    try:
        return SyncState.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError):
        return SyncState()


def write_sync_state(project_root: Path, state: SyncState) -> None:
    """Persist the sync state atomically."""
    path = sync_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    tmp.replace(path)
