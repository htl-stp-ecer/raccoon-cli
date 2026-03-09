"""Project validation and discovery utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Any

import yaml


class ProjectError(Exception):
    """Raised when project validation fails."""
    pass


def find_project_root(start_path: Path | None = None) -> Path:
    """
    Find the project root by looking for raccoon.project.yml.

    Searches upward from start_path (default: current directory) until
    finding raccoon.project.yml or hitting the filesystem root.

    Returns:
        Path to the directory containing raccoon.project.yml

    Raises:
        ProjectError: If no raccoon.project.yml is found
    """
    if start_path is None:
        try:
            start_path = Path.cwd()
        except (FileNotFoundError, OSError) as e:
            raise ProjectError(
                f"Current directory not accessible: {e}\n"
                "Please navigate to a valid directory and try again."
            )

    current = start_path.resolve()

    while True:
        project_file = current / "raccoon.project.yml"
        if project_file.exists():
            return current

        # Check if we've hit the root
        parent = current.parent
        if parent == current:
            raise ProjectError(
                "Not in a project directory. No raccoon.project.yml found.\n"
                "Create a raccoon.project.yml file in your project root."
            )
        current = parent


def load_project_config(project_root: Path | None = None) -> Dict[str, Any]:
    """
    Load and parse the raccoon.project.yml configuration.

    Args:
        project_root: Path to project root. If None, will search for it.

    Returns:
        Parsed project configuration dictionary

    Raises:
        ProjectError: If raccoon.project.yml is invalid or not found
    """
    if project_root is None:
        project_root = find_project_root()

    project_file = project_root / "raccoon.project.yml"

    try:
        from raccoon.yaml_utils import load_yaml

        config = load_yaml(project_file)

        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must contain a YAML mapping")

        return config
    except OSError as e:
        raise ProjectError(f"Cannot read raccoon.project.yml: {e}")
    except Exception as e:
        if isinstance(e, ProjectError):
            raise
        raise ProjectError(f"Invalid YAML in raccoon.project.yml: {e}")


def resolve_config_file(project_root: Path, key: str) -> Path:
    """Return the file that owns a given top-level config *key*.

    Inspects the raw YAML node tree of ``raccoon.project.yml`` to find
    ``!include`` / ``!include-merge`` tags without resolving them.
    Falls back to the main project file.
    """
    from ruamel.yaml import YAML

    project_file = project_root / "raccoon.project.yml"
    yml = YAML()
    with open(project_file, "r", encoding="utf-8") as f:
        tree = yml.compose(f)

    if tree is None or not hasattr(tree, "value"):
        return project_file

    for key_node, value_node in tree.value:
        k = key_node.value
        tag = getattr(value_node, "tag", None)

        # Direct !include for this key
        if k == key and tag == "!include":
            return (project_file.parent / value_node.value).resolve()

        # !include-merge — check if the included file provides the key
        if tag == "!include-merge":
            inc_path = (project_file.parent / value_node.value).resolve()
            if inc_path.exists():
                from raccoon.yaml_utils import load_yaml
                inc_data = load_yaml(inc_path)
                if isinstance(inc_data, dict) and key in inc_data:
                    return inc_path

    return project_file


def require_project() -> Path:
    """
    Ensure we're in a project directory and return the project root.

    This is the main function commands should call to validate they're
    being run in a project context.

    Returns:
        Path to the project root directory

    Raises:
        ProjectError: If not in a project directory
    """
    return find_project_root()
