"""Round-trip YAML utilities that preserve comments and formatting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def _make_yaml() -> YAML:
    """Create a pre-configured round-trip YAML instance."""
    yml = YAML()
    yml.preserve_quotes = True
    yml.default_flow_style = False
    return yml


def load_yaml(path: Path | str) -> dict:
    """Load a YAML file, preserving comments for later round-trip saving.

    Returns a ``CommentedMap`` (dict subclass) when the file contains a
    mapping, or a plain ``dict`` otherwise.
    """
    path = Path(path)
    yml = _make_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yml.load(f)
    return data if data is not None else {}


def save_yaml(data: Any, path: Path | str) -> None:
    """Dump *data* to *path*, preserving any comments attached to the data."""
    path = Path(path)
    yml = _make_yaml()
    with open(path, "w", encoding="utf-8") as f:
        yml.dump(data, f)
