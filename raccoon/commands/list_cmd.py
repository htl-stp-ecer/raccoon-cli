"""List command group for raccoon CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from raccoon.naming import normalize_name
from raccoon.project import ProjectError, load_project_config, find_project_root

logger = logging.getLogger("raccoon")


@click.group(name="list")
def list_command() -> None:
    """List projects and missions."""
    pass


@list_command.command(name="missions")
@click.pass_context
def list_missions_command(ctx: click.Context) -> None:
    """List all missions in the current project."""
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
        missions = config.get('missions', [])
    except ProjectError as exc:
        logger.error(f"Failed to load project config: {exc}")
        raise SystemExit(1) from exc
    
    # Display project info
    console.print(f"\n[bold cyan]Project:[/bold cyan] {project_name}")
    console.print(f"[bold cyan]Location:[/bold cyan] {project_root}\n")
    
    if not missions:
        console.print("[yellow]No missions configured in this project.[/yellow]")
        console.print("[dim]Use 'raccoon create mission <name>' to add missions.[/dim]")
        return
    
    # Create a table for missions
    table = Table(title="Missions", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Mission Class", style="cyan")
    table.add_column("File", style="green")
    table.add_column("Status", style="yellow")
    
    missions_dir = project_root / "src" / "missions"
    
    for idx, mission_entry in enumerate(missions, 1):
        # Handle both dict format (SetupMission: setup) and string format (SetupMission)
        if isinstance(mission_entry, dict):
            mission = list(mission_entry.keys())[0]
        else:
            mission = mission_entry

        # Convert mission class name to file name
        # Remove 'Mission' suffix if present
        mission_base = mission
        if mission_base.endswith('Mission'):
            mission_base = mission_base[:-7]  # Remove 'Mission'
        
        # Convert to snake_case
        mission_snake = normalize_name(mission_base, strip_suffix="").snake
        mission_file = missions_dir / f"{mission_snake}_mission.py"
        
        # Check if file exists
        if mission_file.exists():
            status = "✓ Exists"
            status_style = "green"
        else:
            status = "✗ Missing"
            status_style = "red"
        
        table.add_row(
            str(idx),
            mission,
            f"{mission_snake}_mission.py",
            f"[{status_style}]{status}[/{status_style}]"
        )
    
    console.print(table)
    console.print(f"\n[dim]Total: {len(missions)} mission(s)[/dim]\n")


@list_command.command(name="projects")
@click.option("--path", type=click.Path(), default=".", help="Directory to search for projects")
@click.pass_context
def list_projects_command(ctx: click.Context, path: str) -> None:
    """List all raccoon projects in the specified directory."""
    console: Console = ctx.obj["console"]
    
    search_path = Path(path).resolve()
    
    if not search_path.exists():
        logger.error(f"Path does not exist: {search_path}")
        raise SystemExit(1)
    
    console.print(f"\n[bold cyan]Searching for projects in:[/bold cyan] {search_path}\n")
    
    # Find all raccoon.project.yml files
    projects = []
    
    # Search in the current directory and immediate subdirectories
    for item in search_path.iterdir():
        if item.is_dir():
            project_file = item / "raccoon.project.yml"
            if project_file.exists():
                try:
                    config = load_project_config(item)
                    projects.append({
                        'path': item,
                        'name': config.get('name', 'Unknown'),
                        'uuid': config.get('uuid', 'N/A'),
                        'missions': len(config.get('missions', []))
                    })
                except Exception as e:
                    logger.warning(f"Could not load project at {item}: {e}")
    
    # Also check if the current directory is a project
    current_project_file = search_path / "raccoon.project.yml"
    if current_project_file.exists():
        try:
            config = load_project_config(search_path)
            projects.append({
                'path': search_path,
                'name': config.get('name', 'Unknown'),
                'uuid': config.get('uuid', 'N/A'),
                'missions': len(config.get('missions', []))
            })
        except Exception as e:
            logger.warning(f"Could not load project at {search_path}: {e}")
    
    if not projects:
        console.print("[yellow]No raccoon projects found.[/yellow]")
        console.print("[dim]Use 'raccoon create project <name>' to create a new project.[/dim]")
        return
    
    # Create a table for projects
    table = Table(title="Raccoon Projects", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Project Name", style="cyan")
    table.add_column("Location", style="green")
    table.add_column("Missions", style="yellow", justify="right")
    
    for idx, project in enumerate(projects, 1):
        # Make path relative to search path if possible
        try:
            rel_path = project['path'].relative_to(search_path)
            display_path = f"./{rel_path}" if str(rel_path) != "." else "."
        except ValueError:
            display_path = str(project['path'])
        
        table.add_row(
            str(idx),
            project['name'],
            display_path,
            str(project['missions'])
        )
    
    console.print(table)
    console.print(f"\n[dim]Total: {len(projects)} project(s)[/dim]\n")

