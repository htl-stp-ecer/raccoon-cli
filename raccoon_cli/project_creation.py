"""Shared business logic for creating projects and missions.

Both CLI commands and the IDE backend call these functions directly — no
subprocess indirection.  Raises standard Python exceptions; callers are
responsible for translating them into SystemExit (CLI) or HTTPException (IDE).
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
import subprocess
import uuid as uuid_module
from pathlib import Path
from typing import Tuple

from raccoon_cli.git_history import GitInitResult, initialize_project_history
from raccoon_cli.mission_codegen import get_templates_dir, render_template
from raccoon_cli.mission_config import add_mission_to_config
from raccoon_cli.naming import normalize_name
from raccoon_cli.project import ProjectError, load_project_config

logger = logging.getLogger("raccoon")

EXAMPLE_REPO_URL = "https://github.com/htl-stp-ecer/raccoon-example"

_MISSION_NUMBER_RE = re.compile(r"^[Mm](\d{3})")
_MAX_AUTO_MISSION_NUMBER = 990
_RESERVED_MISSION_NUMBERS = {0, 999}  # M000 = setup, M999 = shutdown


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_next_mission_number(missions: list) -> int:
    """Return the next mission number: next multiple of 10 above the highest
    non-reserved M-prefix (min M010).

    Raises:
        ValueError: if the next number would exceed M990 (give an explicit number).
    """
    highest = 0
    for entry in missions:
        name = list(entry.keys())[0] if isinstance(entry, dict) else str(entry)
        m = _MISSION_NUMBER_RE.match(name)
        if m:
            num = int(m.group(1))
            if num not in _RESERVED_MISSION_NUMBERS:
                highest = max(highest, num)
    next_num = (highest // 10 + 1) * 10
    if next_num > _MAX_AUTO_MISSION_NUMBER:
        raise ValueError(
            f"No automatic mission number available after M{_MAX_AUTO_MISSION_NUMBER}; "
            "specify an explicit prefix, e.g. 'M995Name'."
        )
    return next_num


def scaffold_project(name: str, target_dir: Path) -> Tuple[str, GitInitResult]:
    """Clone example repo, patch project files, init git history.

    Returns ``(project_uuid, git_result)``.

    Raises:
        FileExistsError: if *target_dir* already exists.
        RuntimeError: if cloning fails.
    """
    if target_dir.exists():
        raise FileExistsError(f"Directory '{target_dir}' already exists")

    from raccoon_cli._version import __version__ as cli_version

    _clone_example_project(target_dir, cli_version)

    project_uuid = str(uuid_module.uuid4())
    _patch_project_files(target_dir, name, project_uuid)

    git_result = initialize_project_history(target_dir, name)
    return project_uuid, git_result


def create_mission(project_root: Path, mission_name: str, number: int | None = None) -> str:
    """Create a mission file and register it in the project config.

    Strips any M-prefix and 'Mission' suffix from *mission_name* automatically.
    The mission gets the explicit *number* if given (e.g. ``30`` -> ``M030``),
    otherwise the next free one (highest + 10).
    Returns the generated ``mission_class`` name (e.g. ``M010DriveForwardMission``).

    Raises:
        ValueError: if *number* is reserved (setup/shutdown) or already used.
        FileExistsError: if the mission file already exists.
        FileNotFoundError: if the mission template is missing.
    """
    mission_name = mission_name.strip()

    m = _MISSION_NUMBER_RE.match(mission_name)
    if m:
        mission_name = mission_name[len(m.group(0)):]

    if mission_name.lower().endswith("mission"):
        mission_name = mission_name[:-7]

    try:
        config = load_project_config(project_root)
        project_name = config.get("name", "Raccoon Project")
        existing_missions = config.get("missions", [])
        if not isinstance(existing_missions, list):
            existing_missions = []
    except ProjectError:
        project_name = "Raccoon Project"
        existing_missions = []

    if number is None:
        mission_num = get_next_mission_number(existing_missions)
    else:
        mission_num = number
        if mission_num in _RESERVED_MISSION_NUMBERS:
            raise ValueError(
                f"M{mission_num:03d} is reserved for the setup/shutdown mission; pick another number."
            )
        for entry in existing_missions:
            existing = list(entry.keys())[0] if isinstance(entry, dict) else str(entry)
            m_existing = _MISSION_NUMBER_RE.match(existing)
            if m_existing and int(m_existing.group(1)) == mission_num:
                raise ValueError(f"M{mission_num:03d} is already used by '{existing}'.")
    mission_prefix = f"M{mission_num:03d}"

    nn = normalize_name(mission_name, strip_suffix="")
    mission_pascal = f"{mission_prefix}{nn.pascal}"
    mission_snake = f"m{mission_num:03d}_{nn.snake}"
    mission_class = f"{mission_pascal}Mission"

    mission_file = project_root / "src" / "missions" / f"{mission_snake}_mission.py"
    if mission_file.exists():
        raise FileExistsError(f"Mission file already exists: {mission_file}")

    templates_dir = get_templates_dir()
    mission_template = templates_dir / "mission" / "src" / "missions"
    if not mission_template.exists():
        raise FileNotFoundError(f"Mission template not found at {mission_template}")

    context = {
        "mission_snake_case": mission_snake,
        "mission_pascal_case": mission_pascal,
        "project_name": project_name,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    template_file = mission_template / "{{mission_snake_case}}_mission.py.jinja"
    render_template(template_file, mission_file, context)

    add_mission_to_config(project_root, mission_class)
    return mission_class


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _clone_example_project(target_dir: Path, cli_version: str) -> None:
    ref = f"v{cli_version}"

    check = subprocess.run(
        ["git", "ls-remote", "--tags", EXAMPLE_REPO_URL, ref],
        capture_output=True,
        text=True,
    )
    tag_exists = check.returncode == 0 and check.stdout.strip()

    cmd = ["git", "clone", "--depth", "1"]
    if tag_exists:
        cmd += ["--branch", ref]
        logger.info("Cloning example at tag %s...", ref)
    else:
        logger.info("Tag %s not found — cloning default branch...", ref)
    cmd += [EXAMPLE_REPO_URL, str(target_dir)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to clone example repository: {result.stderr.strip()}"
        )

    shutil.rmtree(target_dir / ".git", ignore_errors=True)


def _patch_project_files(target_dir: Path, name: str, project_uuid: str) -> None:
    project_yml = target_dir / "raccoon.project.yml"
    if project_yml.exists():
        text = project_yml.read_text(encoding="utf-8")
        text = re.sub(r"^name:.*$", f"name: {name}", text, flags=re.MULTILINE)
        text = re.sub(r"^uuid:.*$", f"uuid: {project_uuid}", text, flags=re.MULTILINE)
        project_yml.write_text(text, encoding="utf-8")

    pyproject = target_dir / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        snake_name = normalize_name(name, strip_suffix="").snake
        text = re.sub(
            r'(\[project\][^\[]*?\bname\s*=\s*)"[^"]*"',
            lambda m: m.group(0).rsplit('"', 2)[0] + f'"{snake_name}"',
            text,
            flags=re.DOTALL,
        )
        pyproject.write_text(text, encoding="utf-8")
