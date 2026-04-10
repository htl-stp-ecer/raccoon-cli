"""Create project and mission commands."""

from __future__ import annotations

import logging
import os
import re
import uuid as uuid_module
from pathlib import Path
from typing import Dict, Any

import click
import yaml
from rich.console import Console

from raccoon_cli.git_history import initialize_project_history
from raccoon_cli.mission_codegen import (
    get_templates_dir, copy_template_dir, render_template,
    add_mission_import_to_main,
)
from raccoon_cli.mission_config import add_mission_to_config
from raccoon_cli.naming import normalize_name
from raccoon_cli.project import ProjectError, load_project_config, find_project_root

logger = logging.getLogger("raccoon")



_MISSION_NUMBER_RE = re.compile(r'^[Mm](\d{3})')


def _get_next_mission_number(missions: list) -> int:
    """Return the next mission number (highest existing M-prefix + 10, or 0 if none)."""
    max_num = -10
    for entry in missions:
        name = list(entry.keys())[0] if isinstance(entry, dict) else str(entry)
        m = _MISSION_NUMBER_RE.match(name)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max(0, max_num + 10)







def _open_pycharm_with_instructions(console: Console, project_root: Path) -> None:
    """Open PyCharm and show SSH interpreter setup instructions."""
    from raccoon_cli.ide.launcher import PyCharmLauncher

    # Open PyCharm
    launcher = PyCharmLauncher()
    if launcher.is_available():
        console.print("[cyan]Opening PyCharm...[/cyan]")
        if launcher.launch(project_root):
            console.print("[green]PyCharm launched![/green]")
        else:
            console.print("[yellow]Failed to launch PyCharm automatically.[/yellow]")
            console.print(f"Open the project manually: {project_root}")
    else:
        console.print("[yellow]PyCharm not found in PATH.[/yellow]")
        console.print(f"Open the project manually: {project_root}")

    # Show SSH interpreter setup instructions
    console.print()
    console.print("[bold]To set up the SSH Python interpreter:[/bold]")
    console.print("  1. Run [cyan]raccoon connect <PI_ADDRESS>[/cyan] to connect to your Pi")
    console.print("  2. In PyCharm, follow the SSH interpreter setup guide:")
    console.print("     [link=https://www.jetbrains.com/help/pycharm/configuring-remote-interpreters-via-ssh.html]https://www.jetbrains.com/help/pycharm/configuring-remote-interpreters-via-ssh.html[/link]")
    console.print()
    console.print("[dim]Use your Pi's IP address, username 'pi', and interpreter path '/usr/bin/python3'[/dim]")





@click.group(name="create")
def create_command() -> None:
    """Create projects and missions."""
    pass


@create_command.command(name="project")
@click.argument("name")
@click.option("--path", type=click.Path(), default=".", help="Directory to create project in")
@click.option("--no-wizard", is_flag=True, help="Skip the setup wizard (not recommended)")
@click.option("--no-open-ide", is_flag=True, help="Do not launch PyCharm after creating the project")
@click.pass_context
def create_project_command(ctx: click.Context, name: str, path: str, no_wizard: bool, no_open_ide: bool) -> None:
    """Create a new raccoon project with the given NAME."""
    console: Console = ctx.obj["console"]

    # Resolve the target directory
    target_dir = Path(path).resolve() / name

    if target_dir.exists():
        logger.error(f"Directory {target_dir} already exists")
        raise SystemExit(1)

    console.print(f"[cyan]Creating new project '{name}' at {target_dir}...[/cyan]")

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Get templates directory
    templates_dir = get_templates_dir()
    project_template = templates_dir / "project_scaffold"

    if not project_template.exists():
        logger.error(f"Project template not found at {project_template}")
        raise SystemExit(1)

    # Generate UUID for project
    project_uuid = str(uuid_module.uuid4())

    # Prepare template context
    import datetime
    context = {
        'project_id': normalize_name(name, strip_suffix="").snake,
        'project_name': name,
        'project_uuid': project_uuid,
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # Copy and render templates (includes raccoon.project.yml)
    copy_template_dir(project_template, target_dir, context)

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

    if no_open_ide:
        console.print("[dim]Skipping IDE launch.[/dim]")
        return

    # Open PyCharm and show setup instructions
    _open_pycharm_with_instructions(console, target_dir)


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

    # Add import to main.py
    add_mission_import_to_main(project_root, mission_snake, mission_pascal)

    console.print(f"[green]✓ Mission '{mission_class}' created successfully[/green]")
    console.print(f"[cyan]  File: {mission_file.relative_to(project_root)}[/cyan]")
    console.print(f"[cyan]  Added to raccoon.project.yml[/cyan]")
