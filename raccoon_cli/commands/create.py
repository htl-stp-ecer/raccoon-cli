"""Create project and mission commands."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid as uuid_module
from pathlib import Path
from typing import Dict, Any

import click
from rich.console import Console

from raccoon_cli.git_history import initialize_project_history
from raccoon_cli.mission_codegen import (
    get_templates_dir, render_template,
)
from raccoon_cli.mission_config import add_mission_to_config
from raccoon_cli.naming import normalize_name
from raccoon_cli.project import ProjectError, load_project_config, find_project_root

logger = logging.getLogger("raccoon")

EXAMPLE_REPO_URL = "https://github.com/htl-stp-ecer/raccoon-example"



_MISSION_NUMBER_RE = re.compile(r'^[Mm](\d{3})')


_RESERVED_MISSION_NUMBERS = {0, 999}  # M000 = setup (always first), M999 = shutdown (always last)


def _get_next_mission_number(missions: list) -> int:
    """Return the next mission number (highest non-reserved M-prefix + 10, min M010)."""
    highest = 0  # produces 10 when no non-reserved missions exist
    for entry in missions:
        name = list(entry.keys())[0] if isinstance(entry, dict) else str(entry)
        m = _MISSION_NUMBER_RE.match(name)
        if m:
            num = int(m.group(1))
            if num not in _RESERVED_MISSION_NUMBERS:
                highest = max(highest, num)
    return highest + 10












def _clone_example_project(target_dir: Path, cli_version: str, console: Console) -> None:
    """Clone raccoon-example, preferring the tag matching cli_version."""
    ref = f"v{cli_version}"

    # Check whether the versioned tag exists on the remote
    check = subprocess.run(
        ["git", "ls-remote", "--tags", EXAMPLE_REPO_URL, ref],
        capture_output=True, text=True,
    )
    tag_exists = check.returncode == 0 and check.stdout.strip()

    cmd = ["git", "clone", "--depth", "1"]
    if tag_exists:
        cmd += ["--branch", ref]
        console.print(f"[dim]Cloning example at tag {ref}...[/dim]")
    else:
        console.print(f"[dim]Tag {ref} not found — cloning default branch...[/dim]")
    cmd += [EXAMPLE_REPO_URL, str(target_dir)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        console.print(f"[red]Failed to clone example repository.[/red]")
        console.print(f"[red]{stderr}[/red]")
        console.print("[yellow]Make sure you have an internet connection and git is installed.[/yellow]")
        raise SystemExit(1)

    # Remove cloned .git — we'll init a fresh history below
    shutil.rmtree(target_dir / ".git", ignore_errors=True)


def _patch_project_files(target_dir: Path, name: str, project_uuid: str) -> None:
    """Update name/uuid in raccoon.project.yml and pyproject.toml after cloning."""
    project_yml = target_dir / "raccoon.project.yml"
    if project_yml.exists():
        text = project_yml.read_text(encoding="utf-8")
        # Replace the name and uuid lines (YAML with !include tags — edit as text)
        text = re.sub(r'^name:.*$', f'name: {name}', text, flags=re.MULTILINE)
        text = re.sub(r'^uuid:.*$', f'uuid: {project_uuid}', text, flags=re.MULTILINE)
        project_yml.write_text(text, encoding="utf-8")

    pyproject = target_dir / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        snake_name = normalize_name(name, strip_suffix="").snake
        # Replace name only inside the [project] section
        text = re.sub(
            r'(\[project\][^\[]*?\bname\s*=\s*)"[^"]*"',
            lambda m: m.group(0).rsplit('"', 2)[0] + f'"{snake_name}"',
            text,
            flags=re.DOTALL,
        )
        pyproject.write_text(text, encoding="utf-8")


@click.group(name="create")
def create_command() -> None:
    """Create projects and missions."""
    pass


@create_command.command(name="project")
@click.argument("name")
@click.option("--path", type=click.Path(), default=".", help="Directory to create project in")
@click.option("--no-wizard", is_flag=True, help="Skip the setup wizard (not recommended)")
@click.pass_context
def create_project_command(ctx: click.Context, name: str, path: str, no_wizard: bool) -> None:
    """Create a new raccoon project with the given NAME."""
    console: Console = ctx.obj["console"]

    from raccoon_cli._version import __version__ as cli_version

    # Resolve the target directory
    target_dir = Path(path).resolve() / name

    if target_dir.exists():
        logger.error(f"Directory {target_dir} already exists")
        raise SystemExit(1)

    console.print(f"[cyan]Creating new project '{name}' at {target_dir}...[/cyan]")

    # Clone the example repo (creates target_dir)
    _clone_example_project(target_dir, cli_version, console)

    # Generate UUID and patch name/uuid into project files
    project_uuid = str(uuid_module.uuid4())
    _patch_project_files(target_dir, name, project_uuid)

    console.print(f"[green]✓ Project '{name}' scaffolded at {target_dir}[/green]")
    console.print(f"[cyan]Project UUID: {project_uuid}[/cyan]")

    history_result = initialize_project_history(target_dir, name)
    if history_result.commit_created:
        console.print("[green]✓ Local git history initialized[/green]")
        console.print(f"[dim]Initial snapshot: {history_result.commit_sha}[/dim]")
    elif history_result.reason == "git_unavailable":
        console.print("[yellow]Git is not installed. Skipping local history initialization.[/yellow]")
    elif history_result.reason not in {"already_git_repo", "no_changes"}:
        console.print(f"[yellow]Warning: Could not initialize local git history ({history_result.error})[/yellow]")

    # Run wizard before finalizing (unless explicitly skipped)
    if not no_wizard:
        console.print("\n[cyan]Launching setup wizard to finalize project configuration...[/cyan]\n")

        from raccoon_cli.commands.wizard import wizard_command

        # Change to project directory and run wizard
        original_cwd = os.getcwd()
        try:
            os.chdir(target_dir)
            ctx.invoke(wizard_command, dry_run=False)
        finally:
            os.chdir(original_cwd)

        console.print(f"\n[green]✓ Project '{name}' finalized successfully![/green]")
    else:
        console.print(f"\n[yellow]Wizard skipped. Run 'cd {target_dir} && raccoon wizard' to configure your project.[/yellow]")



@create_command.command(name="mission")
@click.argument("name")
@click.pass_context
def create_mission_command(ctx: click.Context, name: str) -> None:
    """Create a new mission with the given NAME in the current project."""
    console: Console = ctx.obj["console"]

    # Ensure we're in a project
    try:
        project_root = find_project_root()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    # Load project config
    try:
        config = load_project_config(project_root)
        project_name = config.get('name', 'Raccoon Project')
        existing_missions = config.get('missions', [])
        if not isinstance(existing_missions, list):
            existing_missions = []
    except ProjectError:
        project_name = 'Raccoon Project'
        existing_missions = []

    # Strip M-prefix if user already provided one (we'll re-add it)
    original_name = name
    m = _MISSION_NUMBER_RE.match(name)
    if m:
        name = name[len(m.group(0)):]
        console.print(f"[dim]Note: Stripped M-prefix from input — will be auto-assigned.[/dim]")

    # Strip 'Mission' suffix if present
    if name.lower().endswith('mission'):
        name = name[:-7]
        console.print(f"[yellow]Note: Removed 'Mission' suffix from name.[/yellow]")
        console.print(f"[dim]  Input: '{original_name}' → Using: '{name}'[/dim]")

    # Assign the next sequential mission number (M000, M010, M020 …)
    mission_num = _get_next_mission_number(existing_missions)
    mission_prefix = f"M{mission_num:03d}"

    # Build names: M010DriveToSmth / m010_drive_to_smth
    nn = normalize_name(name, strip_suffix="")
    name_pascal = nn.pascal
    name_snake = nn.snake
    mission_pascal = f"{mission_prefix}{name_pascal}"   # M010DriveToSmth
    mission_snake = f"m{mission_num:03d}_{name_snake}"  # m010_drive_to_smth
    mission_class = f"{mission_pascal}Mission"           # M010DriveToSmthMission

    console.print(f"[cyan]Creating mission '{mission_class}'...[/cyan]")

    # Check if mission already exists
    mission_file = project_root / "src" / "missions" / f"{mission_snake}_mission.py"
    if mission_file.exists():
        logger.error(f"Mission file already exists: {mission_file}")
        raise SystemExit(1)

    # Get templates directory
    templates_dir = get_templates_dir()
    mission_template = templates_dir / "mission" / "src" / "missions"

    if not mission_template.exists():
        logger.error(f"Mission template not found at {mission_template}")
        raise SystemExit(1)

    # Prepare template context
    import datetime
    context = {
        'mission_snake_case': mission_snake,
        'mission_pascal_case': mission_pascal,
        'project_name': project_name,
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # Render mission template
    template_file = mission_template / "{{mission_snake_case}}_mission.py.jinja"
    render_template(template_file, mission_file, context)

    # Add mission to project config
    add_mission_to_config(project_root, mission_class)

    console.print(f"[green]✓ Mission '{mission_class}' created successfully[/green]")
    console.print(f"[cyan]  File: {mission_file.relative_to(project_root)}[/cyan]")
    console.print(f"[cyan]  Added to raccoon.project.yml[/cyan]")
