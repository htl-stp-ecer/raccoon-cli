"""Create project and mission commands."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid as uuid_module
from pathlib import Path
from typing import Dict, Any, Optional

import click
import yaml
from jinja2 import Environment, FileSystemLoader
from rich.console import Console

from raccoon.project import ProjectError, load_project_config, find_project_root

logger = logging.getLogger("raccoon")


def _prompt_and_connect_to_pi(console: Console, project_root: Path) -> Optional["RaccoonApiClient"]:
    """
    Prompt the user for Pi connection details and establish connection.

    Returns an API client if connection is successful, None otherwise.
    """
    from raccoon.client.api import create_api_client
    from raccoon.client.connection import get_connection_manager
    from raccoon.client.discovery import check_address

    console.print("\n[bold cyan]Pi Connection Setup[/bold cyan]")
    console.print("To calibrate encoders, the wizard needs to connect to your Pi.\n")

    if not click.confirm("Connect to a Pi for encoder calibration?", default=True):
        console.print("[yellow]Skipping Pi connection. Encoder calibration will use manual entry.[/yellow]")
        return None

    # Get Pi address
    address = click.prompt("Pi address (IP or hostname)", default="192.168.4.1")
    port = click.prompt("Pi server port", default=8421, type=int)
    user = click.prompt("SSH username", default="pi")

    # Check if the Pi is reachable
    console.print(f"\n[cyan]Checking connection to {address}:{port}...[/cyan]")
    result = asyncio.run(check_address(address, port))

    if not result:
        console.print(f"[red]Failed to connect to {address}:{port}[/red]")
        console.print("Make sure the Pi is running and raccoon-server is started.")
        console.print("[yellow]Continuing without Pi connection. Encoder calibration will use manual entry.[/yellow]")
        return None

    # Connect using connection manager
    manager = get_connection_manager()
    success = asyncio.run(manager.connect(address=address, port=port, user=user))

    if not success:
        console.print(f"[red]Failed to connect to {address}:{port}[/red]")
        console.print("[yellow]Continuing without Pi connection. Encoder calibration will use manual entry.[/yellow]")
        return None

    state = manager.state
    console.print(f"[green]Connected to {state.pi_hostname}[/green]")

    # Check API token
    if not state.api_token:
        console.print("[yellow]SSH key authentication failed. Cannot access hardware.[/yellow]")
        console.print("[yellow]Continuing without Pi connection. Encoder calibration will use manual entry.[/yellow]")
        return None

    # Save connection to project config
    manager.save_to_project(project_root)
    manager.save_to_global()
    console.print(f"[dim]Connection saved to project config[/dim]")

    # Create and return API client
    return create_api_client(address, port, state.api_token)


def _to_snake_case(name: str) -> str:
    """Convert a string to snake_case."""
    # Replace hyphens and spaces with underscores
    name = re.sub(r'[-\s]+', '_', name)
    # Insert underscores before uppercase letters and convert to lowercase
    name = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    # Remove any duplicate underscores
    name = re.sub(r'_+', '_', name)
    return name.strip('_')


def _to_pascal_case(name: str) -> str:
    """Convert a string to PascalCase."""
    # First convert to snake_case to handle camelCase/PascalCase inputs
    snake = _to_snake_case(name)
    # Split on underscores, hyphens, and spaces
    words = re.split(r'[-_\s]+', snake)
    # Capitalize first letter of each word
    return ''.join(word.capitalize() for word in words if word)


def _get_templates_dir() -> Path:
    """Get the templates directory path."""
    # Templates are inside the raccoon package at raccoon/templates/
    import raccoon
    package_dir = Path(raccoon.__file__).parent
    templates_dir = package_dir / "templates"

    if templates_dir.exists():
        return templates_dir

    raise ProjectError(
        f"Templates directory not found at {templates_dir}.\n"
        f"Please reinstall the raccoon package."
    )


def _render_template(template_path: Path, output_path: Path, context: Dict[str, Any]) -> None:
    """Render a Jinja2 template file to an output path."""
    # Set up Jinja2 environment
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        extensions=['jinja2_time.TimeExtension']
    )
    
    # Load and render the template
    template_name = template_path.name
    template = env.get_template(template_name)
    rendered = template.render(**context)
    
    # Write to output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding='utf-8')


def _copy_template_dir(template_dir: Path, target_dir: Path, context: Dict[str, Any]) -> None:
    """
    Recursively copy and render a template directory.
    
    Files ending with .jinja are rendered as templates.
    Other files are copied as-is.
    Filenames with {{...}} are also rendered.
    """
    for item in template_dir.rglob('*'):
        if item.is_file():
            # Skip copier.yaml and other metadata files
            if item.name in ['copier.yaml', 'codemods.yaml.jinja']:
                continue
            
            # Get relative path
            rel_path = item.relative_to(template_dir)
            
            # Render the output path (handle {{...}} in filenames)
            output_path_str = str(rel_path)
            for key, value in context.items():
                output_path_str = output_path_str.replace(f"{{{{{key}}}}}", str(value))
            
            output_path = target_dir / output_path_str
            
            # Check if it's a Jinja template
            if item.suffix == '.jinja':
                # Remove .jinja extension from output
                output_path = output_path.with_suffix('')
                _render_template(item, output_path, context)
            else:
                # Copy file as-is
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, output_path)


def _add_mission_to_project_config(project_root: Path, mission_class: str) -> None:
    """Add a mission to the raccoon.project.yml file."""
    config_path = project_root / "raccoon.project.yml"
    
    # Load existing config
    config = load_project_config(project_root)
    
    # Get or create missions list
    missions = config.get('missions', [])
    if not isinstance(missions, list):
        missions = []
    
    # Add mission if not already present
    if mission_class not in missions:
        missions.append(mission_class)
        config['missions'] = missions
        
        # Write back to file
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(config, f, sort_keys=False)



def _open_pycharm_with_instructions(console: Console, project_root: Path) -> None:
    """Open PyCharm and show SSH interpreter setup instructions."""
    from raccoon.ide.launcher import PyCharmLauncher

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


def _add_mission_import_to_main(project_root: Path, mission_snake: str, mission_pascal: str) -> None:
    """Add mission import and registration to main.py."""
    main_py = project_root / "src" / "main.py"
    
    if not main_py.exists():
        logger.warning(f"main.py not found at {main_py}")
        return
    
    content = main_py.read_text(encoding='utf-8')
    
    # Add import after other mission imports
    import_line = f"from .missions.{mission_snake}_mission import {mission_pascal}Mission"
    
    if import_line not in content:
        # Find where to add the import (after other mission imports)
        lines = content.split('\n')
        insert_idx = 0
        
        for i, line in enumerate(lines):
            if 'from .missions.' in line and 'import' in line:
                insert_idx = i + 1
        
        if insert_idx == 0:
            # No mission imports found, add after regular imports
            for i, line in enumerate(lines):
                if line.strip() and not line.startswith('#') and not line.startswith('"""') and 'import' not in line:
                    insert_idx = i
                    break
        
        lines.insert(insert_idx, import_line)
        content = '\n'.join(lines)
        main_py.write_text(content, encoding='utf-8')



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

    # Resolve the target directory
    target_dir = Path(path).resolve() / name

    if target_dir.exists():
        logger.error(f"Directory {target_dir} already exists")
        raise SystemExit(1)

    console.print(f"[cyan]Creating new project '{name}' at {target_dir}...[/cyan]")

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Get templates directory
    templates_dir = _get_templates_dir()
    project_template = templates_dir / "project_scaffold"

    if not project_template.exists():
        logger.error(f"Project template not found at {project_template}")
        raise SystemExit(1)

    # Generate UUID for project
    project_uuid = str(uuid_module.uuid4())

    # Prepare template context
    import datetime
    context = {
        'project_id': _to_snake_case(name),
        'project_name': name,
        'project_uuid': project_uuid,
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # Copy and render templates (includes raccoon.project.yml)
    _copy_template_dir(project_template, target_dir, context)

    console.print(f"[green]✓ Project '{name}' scaffolded at {target_dir}[/green]")
    console.print(f"[cyan]Project UUID: {project_uuid}[/cyan]")

    # Run wizard before finalizing (unless explicitly skipped)
    if not no_wizard:
        # Prompt for Pi connection before running wizard
        api_client = _prompt_and_connect_to_pi(console, target_dir)

        console.print("\n[cyan]Launching setup wizard to finalize project configuration...[/cyan]\n")

        # Set up the API client for remote encoder reading
        from raccoon.commands.wizard import wizard_command, set_api_client, clear_api_client

        if api_client:
            set_api_client(api_client)

        # Change to project directory and run wizard
        original_cwd = os.getcwd()
        try:
            os.chdir(target_dir)
            ctx.invoke(wizard_command, dry_run=False)
        finally:
            os.chdir(original_cwd)
            clear_api_client()

        console.print(f"\n[green]✓ Project '{name}' finalized successfully![/green]")
    else:
        console.print(f"\n[yellow]Wizard skipped. Run 'cd {target_dir} && raccoon wizard' to configure your project.[/yellow]")

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
    except ProjectError:
        project_name = 'Raccoon Project'
    
    # Check if user accidentally included "Mission" suffix
    original_name = name
    if name.lower().endswith('mission'):
        name = name[:-7]

        console.print(f"[yellow]Note: Removed 'Mission' suffix from name.[/yellow]")
        console.print(f"[dim]  Input: '{original_name}' → Using: '{name}'[/dim]")

    # Convert name to snake_case and PascalCase
    mission_snake = _to_snake_case(name)
    mission_pascal = _to_pascal_case(name)
    mission_class = f"{mission_pascal}Mission"
    
    console.print(f"[cyan]Creating mission '{mission_class}'...[/cyan]")
    
    # Check if mission already exists
    mission_file = project_root / "src" / "missions" / f"{mission_snake}_mission.py"
    if mission_file.exists():
        logger.error(f"Mission file already exists: {mission_file}")
        raise SystemExit(1)
    
    # Get templates directory
    templates_dir = _get_templates_dir()
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
    _render_template(template_file, mission_file, context)
    
    # Add mission to project config
    _add_mission_to_project_config(project_root, mission_class)
    
    # Add import to main.py
    _add_mission_import_to_main(project_root, mission_snake, mission_pascal)
    
    console.print(f"[green]✓ Mission '{mission_class}' created successfully[/green]")
    console.print(f"[cyan]  File: {mission_file.relative_to(project_root)}[/cyan]")
    console.print(f"[cyan]  Added to raccoon.project.yml[/cyan]")


