"""Camera backends and processors on the robot: the built-in ones and those in projects.

Shared logic for the Pi server (and anything else that wants it, per the service-layer rule in CLAUDE.md).

* Projects are scanned with ``ast``, without importing a line of project code: a class that inherits
  ``CamBackend`` or ``CamProcessor`` (directly, by a dotted name, or through another class of the project)
  is one. That gives the list a UI shows.
* What a class can be configured with needs the class imported. That runs ``python -m raccoon_cam.describe``
  in a process of its own, so projects that use the same package name (``src``) never mix, and a project that
  crashes on import cannot take the server down.
* The backend that was chosen is kept by raccoon-cam in ``backend.json`` (``raccoon_cam.state``); the camera
  service watches that file and applies a change itself.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("raccoon")

BACKEND_BASE = "CamBackend"
PROCESSOR_BASE = "CamProcessor"
SKIPPED_DIRECTORIES = {"__pycache__", ".venv", "venv", "node_modules", ".git", "build", "dist"}
DEFAULT_STATE_DIRECTORY = "/var/lib/raccoon-cam"
DESCRIBE_TIMEOUT = 30.0


class CameraUnavailable(RuntimeError):
    """raccoon-cam is not installed on this machine."""


class CameraDiscoveryError(RuntimeError):
    """A class could not be described, or a configuration is wrong. The message says why."""


@dataclass(frozen=True)
class FoundClass:
    """A backend or processor class found in a project's source."""

    kind: str  # "backend" or "processor"
    class_name: str
    module: str  # "src.cam.mine", importable with the project directory on sys.path
    file: str  # relative to the project directory
    name: str  # the class's own `name`, if it is a literal, else the class name
    description: str  # the first line of the docstring

    @property
    def path(self) -> str:
        return f"{self.module}:{self.class_name}"


# -- scanning --------------------------------------------------------------------------------------


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _literal_name(cls: ast.ClassDef) -> str | None:
    for node in cls.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "name" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "name":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def _module_name(relative: Path) -> str | None:
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _python_files(directory: Path):
    for root, directories, files in os.walk(directory):
        directories[:] = sorted(d for d in directories if d not in SKIPPED_DIRECTORIES and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(root) / name


def scan_project(project_dir: Path) -> list[FoundClass]:
    """The backend and processor classes in ``<project>/src`` (the whole project if there is no ``src``).

    A top-level class whose name does not start with ``_`` counts if it inherits ``CamBackend`` or
    ``CamProcessor``, also through other classes of the project. Files that do not parse are skipped.
    """
    project_dir = Path(project_dir)
    root = project_dir / "src" if (project_dir / "src").is_dir() else project_dir
    classes: dict[str, tuple[ast.ClassDef, Path]] = {}
    ordered: list[tuple[str, ast.ClassDef, Path]] = []
    for file in _python_files(root):
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            logger.debug("camera scan: cannot parse %s", file)
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, (node, file))
                ordered.append((node.name, node, file))

    def kind_of(cls: ast.ClassDef, seen: frozenset[str] = frozenset()) -> str | None:
        for base in cls.bases:
            name = _base_name(base)
            if name == BACKEND_BASE:
                return "backend"
            if name == PROCESSOR_BASE:
                return "processor"
            if name and name in classes and name not in seen and name != cls.name:
                inherited = kind_of(classes[name][0], seen | {cls.name})
                if inherited:
                    return inherited
        return None

    found = []
    for class_name, node, file in ordered:
        if class_name.startswith("_"):
            continue
        kind = kind_of(node)
        if kind is None:
            continue
        module = _module_name(file.relative_to(project_dir))
        if module is None:
            continue
        docstring = (ast.get_docstring(node) or "").strip()
        found.append(
            FoundClass(
                kind=kind,
                class_name=class_name,
                module=module,
                file=str(file.relative_to(project_dir)),
                name=_literal_name(node) or class_name,
                description=docstring.splitlines()[0].strip() if docstring else "",
            )
        )
    return found


# -- asking raccoon-cam ----------------------------------------------------------------------------


def _require_raccoon_cam() -> None:
    if importlib.util.find_spec("raccoon_cam") is None:
        raise CameraUnavailable("raccoon-cam is not installed on this robot")


def _run_describe(arguments: list[str], project_dir: Path | None, timeout: float) -> Any:
    _require_raccoon_cam()
    command = [sys.executable, "-m", "raccoon_cam.describe", *arguments]
    if project_dir is not None:
        command += ["--project-dir", str(project_dir)]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, cwd=str(project_dir) if project_dir else None
        )
    except subprocess.TimeoutExpired:
        raise CameraDiscoveryError(f"describing {' '.join(arguments)} took longer than {timeout:.0f} s") from None
    lines = result.stdout.strip().splitlines()
    try:
        document = json.loads(lines[-1]) if lines else None
    except ValueError:
        document = None
    if document is None:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise CameraDiscoveryError(f"raccoon_cam.describe failed: {detail}")
    if result.returncode != 0 or (isinstance(document, dict) and "error" in document):
        raise CameraDiscoveryError(str(document.get("error", "failed")) if isinstance(document, dict) else "failed")
    return document


def builtin_backends(timeout: float = DESCRIBE_TIMEOUT) -> list[dict[str, Any]]:
    """The backends that ship with raccoon-cam, with their options."""
    return _run_describe(["--builtin"], None, timeout)


def describe_class(path: str, project_dir: Path | None = None, kind: str = "backend", timeout: float = DESCRIBE_TIMEOUT) -> dict[str, Any]:
    """One class with its options, imported in a process of its own (``project_dir`` goes on ``sys.path`` there)."""
    return _run_describe([path, "--kind", kind], project_dir, timeout)


def validate_config(
    path: str, cfg: dict[str, Any], project_dir: Path | None = None, kind: str = "backend", timeout: float = DESCRIBE_TIMEOUT
) -> dict[str, Any]:
    """The configuration as the class's declared types. ``CameraDiscoveryError`` says what is wrong with it."""
    return _run_describe([path, "--kind", kind, "--validate", json.dumps(cfg)], project_dir, timeout)["cfg"]


# -- the list a UI shows ---------------------------------------------------------------------------


def origin(project: dict[str, Any] | None) -> dict[str, Any]:
    """``Built-In``, or ``Project (<name>)``: the UI turns this into the text next to a backend."""
    if project is None:
        return {"kind": "builtin", "project": None}
    return {"kind": "project", "project": project["name"]}


def list_backends(projects: list[dict[str, Any]], timeout: float = DESCRIBE_TIMEOUT) -> list[dict[str, Any]]:
    """Built-in backends, then those found in each project (``projects``: ``ProjectManager.list_projects()``)."""
    items = []
    for description in builtin_backends(timeout):
        items.append({**description, "origin": origin(None), "project_id": None, "project_dir": None})
    for project in projects:
        project_dir = Path(project["path"])
        try:
            found = scan_project(project_dir)
        except OSError:
            logger.warning("camera scan: cannot read project %s", project_dir)
            continue
        for item in found:
            if item.kind != "backend":
                continue
            items.append(
                {
                    "path": item.path,
                    "name": item.name,
                    "description": item.description,
                    "origin": origin(project),
                    "project_id": project["id"],
                    "project_dir": str(project_dir),
                    "file": item.file,
                }
            )
    return items


# -- the chosen backend ----------------------------------------------------------------------------


def state_file() -> Path:
    """Where raccoon-cam keeps the chosen backend: its state directory, as the unit's ``STATE_DIRECTORY``."""
    _require_raccoon_cam()
    from raccoon_cam import state

    return state.state_file() or state.state_file(DEFAULT_STATE_DIRECTORY)


def get_choice() -> dict[str, Any] | None:
    from raccoon_cam import state

    choice = state.load(state_file())
    return choice.to_dict() if choice else None


def save_choice(path: str, cfg: dict[str, Any], project_dir: Path | None = None) -> dict[str, Any]:
    """Validate and keep the choice. The camera service applies it on its own. Returns what was saved."""
    from raccoon_cam import state

    coerced = validate_config(path, cfg, project_dir)
    choice = state.BackendChoice(path=path, cfg=coerced, project_dir=str(project_dir) if project_dir else None)
    try:
        state.save(state_file(), choice)
    except OSError as exc:
        raise CameraDiscoveryError(
            f"cannot save the camera backend ({exc}). Is raccoon-cam up to date (it creates the state directory)?"
        ) from exc
    return choice.to_dict()


def clear_choice() -> None:
    from raccoon_cam import state

    state.clear(state_file())
