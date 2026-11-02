"""Laptop-side update logic: pin bundle versions in a project's pyproject.toml.

The laptop packages (raccoon-cli, raccoon-lib) live in the project's uv
environment, so updating means rewriting the pins in ``pyproject.toml`` and
running ``uv sync``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

# Distribution names (normalized) that are pinned in the project's pyproject.
PINNED_DISTRIBUTIONS = ("raccoon-cli", "raccoon-library")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pin_dependency(text: str, dist_name: str, version: str) -> tuple[str, bool]:
    """Replace the version specifier of ``dist_name`` with ``==version``.

    Extras are kept (``raccoon-library[sim]>=1`` -> ``raccoon-library[sim]==2``).
    Returns the new text and whether a dependency entry was found.
    """
    target = _normalize(dist_name)
    found = False

    def repl(m: re.Match) -> str:
        nonlocal found
        if _normalize(m.group("name")) != target:
            return m.group(0)
        found = True
        q = m.group("q")
        return f'{q}{m.group("name")}{m.group("extras") or ""}=={version}{q}'

    pattern = re.compile(
        r"(?P<q>[\"'])(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?[^\"'\n]*(?P=q)"
    )
    return pattern.sub(repl, text), found


def set_project_version(text: str, version: str) -> tuple[str, bool]:
    """Set ``version = "..."`` inside the ``[project]`` table."""
    m = re.search(r"^\[project\]\s*$(?P<body>.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return text, False
    body = m.group("body")
    new_body, n = re.subn(
        r'^(version\s*=\s*)(["\'])[^"\']*\2',
        lambda v: f'{v.group(1)}"{version}"',
        body,
        count=1,
        flags=re.MULTILINE,
    )
    if not n:
        return text, False
    return text[: m.start("body")] + new_body + text[m.end("body"):], True


def is_valid_project_version(version: str) -> bool:
    """True if ``version`` is PEP 440 (e.g. ``2026.4.25.1``, not ``dev``)."""
    try:
        from packaging.version import Version

        Version(version)
        return True
    except Exception:
        return False


def running_inside_project_env(project_root: Path) -> bool:
    """True if this interpreter lives in the project's own virtualenv."""
    try:
        prefix = Path(sys.prefix).resolve()
        return prefix.is_relative_to(project_root.resolve())
    except Exception:
        return False


def can_sync_while_running(project_root: Path) -> bool:
    """Whether ``uv sync`` may replace packages of the running CLI.

    POSIX lets files of a running process be replaced. Windows locks the
    running ``raccoon.exe``, so syncing the env we run from must be manual.
    """
    if sys.platform != "win32":
        return True
    return not running_inside_project_env(project_root)


def apply_pins(
    pyproject: Path,
    pins: dict[str, str],
    bundle_version: Optional[str],
) -> tuple[list[str], list[str]]:
    """Rewrite ``pyproject`` with the pinned versions.

    ``pins`` maps distribution name -> version. Returns (changed, missing):
    the dependencies that were rewritten and those with no entry in the file.
    """
    text = pyproject.read_text(encoding="utf-8")
    changed: list[str] = []
    missing: list[str] = []
    for dist, version in pins.items():
        text, found = pin_dependency(text, dist, version)
        (changed if found else missing).append(dist)
    if bundle_version and is_valid_project_version(bundle_version):
        text, _ = set_project_version(text, bundle_version)
    pyproject.write_text(text, encoding="utf-8")
    return changed, missing
