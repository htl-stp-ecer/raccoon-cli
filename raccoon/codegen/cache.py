"""Caching utilities for raccoon code generation."""

from __future__ import annotations

import inspect
import json
import logging
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("raccoon")

CACHE_FILENAME = ".raccoon_codegen_cache.json"
CACHE_VERSION = 1


def _json_default(obj: Any) -> str:
    """JSON serializer for unsupported types."""
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


def hash_payload(payload: Any) -> str:
    """Create a stable SHA-256 hash for an arbitrary payload."""
    try:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except TypeError:
        serialized = repr(payload)
    return sha256(serialized.encode("utf-8")).hexdigest()


def source_fingerprint(obj: Any) -> str:
    """Return a hash of an object's source code, if available."""
    try:
        source = inspect.getsource(obj)
    except (OSError, TypeError):
        return ""
    return sha256(source.encode("utf-8")).hexdigest()


class CodegenCache:
    """Persisted cache of generator fingerprints keyed by output file."""

    def __init__(self, base_dir: Path):
        self.cache_file = base_dir / CACHE_FILENAME
        self._data: Dict[str, Any] = {"version": CACHE_VERSION, "entries": {}}
        self._load()

    def _load(self) -> None:
        if not self.cache_file.exists():
            return

        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Failed to read cache file {self.cache_file}: {exc}")
            return

        if not isinstance(raw, dict):
            logger.warning(f"Ignoring malformed cache file {self.cache_file}")
            return

        if raw.get("version") != CACHE_VERSION:
            logger.debug(
                "Cache version mismatch for %s (expected %s, found %s)",
                self.cache_file,
                CACHE_VERSION,
                raw.get("version"),
            )
            return

        entries = raw.get("entries")
        if not isinstance(entries, dict):
            logger.debug(
                "Cache entries missing or invalid in %s; ignoring cache",
                self.cache_file,
            )
            return

        self._data["entries"] = entries

    def get(self, key: str) -> Dict[str, Any] | None:
        """Return cache entry for key, if present."""
        entry = self._data["entries"].get(key)
        if isinstance(entry, dict):
            return entry
        return None

    def set(self, key: str, value: Dict[str, Any]) -> None:
        """Persist cache entry for key."""
        if self._data["entries"].get(key) == value:
            return
        self._data["entries"][key] = value
        self._save()

    def clear(self) -> None:
        """Clear all cached entries."""
        self._data["entries"] = {}
        self._save()

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(self._data, indent=2, sort_keys=True)
            self.cache_file.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Unable to write cache file {self.cache_file}: {exc}")
