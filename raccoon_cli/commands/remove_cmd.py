"""Remove command group for raccoon CLI."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import click
from rich.console import Console

from raccoon_cli.mission_codegen import remove_mission_import_from_main
from raccoon_cli.mission_config import remove_mission_from_config
from raccoon_cli.naming import normalize_name
from raccoon_cli.project import ProjectError, load_project_config, find_project_root

logger = logging.getLogger("raccoon")





@click.group(name="remove")
def remove_command() -> None:
    """Remove projects and missions."""
    pass


@remove_command.command(name="mission")
@click.argument("name")
@click.option("--keep-file", is_flag=True, help="Keep the mission file, only remove from config")
@click.pass_context
def remove_mission_command(ctx: click.Context, name: str, keep_file: bool) -> None:
    """Remove a mission with the given NAME from the current project."""
    console: Console = ctx.obj["console"]
    
    # Ensure we're in a project
    try:
        project_root = find_project_root()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    
    # Check if user accidentally included "Mission" suffix
    original_name = name
    if name.lower().endswith('mission'):
        name = name[:-7]
        console.print(f"[yellow]Note: Removed 'Mission' suffix from name.[/yellow]")
        console.print(f"[dim]  Input: '{original_name}' → Using: '{name}'[/dim]")

    # Convert name to snake_case and PascalCase
    nn = normalize_name(name, strip_suffix="")
    mission_snake = nn.snake
    mission_pascal = nn.pascal
    mission_class = f"{mission_pascal}Mission"
    
    console.print(f"[cyan]Removing mission '{mission_class}'...[/cyan]")
    
    # Remove from project config
    removed_from_config = remove_mission_from_config(project_root, mission_class)
    
    if not removed_from_config:
        logger.warning(f"Mission '{mission_class}' not found in raccoon.project.yml")
    else:
        console.print(f"[green]✓ Removed '{mission_class}' from raccoon.project.yml[/green]")
    
    # Remove import from main.py
    remove_mission_import_from_main(project_root, mission_snake, mission_pascal)
    console.print(f"[green]✓ Removed import from main.py[/green]")
    
    # Remove mission file unless --keep-file is set
    if not keep_file:
        mission_file = project_root / "src" / "missions" / f"{mission_snake}_mission.py"
        if mission_file.exists():
            mission_file.unlink()
            console.print(f"[green]✓ Deleted {mission_file.relative_to(project_root)}[/green]")
        else:
            logger.warning(f"Mission file not found: {mission_file}")
    else:
        console.print(f"[yellow]Mission file kept (use --keep-file=false to delete)[/yellow]")
    
    console.print(f"[green]✓ Mission '{mission_class}' removed successfully[/green]")


@remove_command.command(name="project")
@click.argument("name")
@click.option("--path", type=click.Path(), default=".", help="Directory containing the project")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def remove_project_command(ctx: click.Context, name: str, path: str, force: bool) -> None:
    """Remove a project with the given NAME."""
    console: Console = ctx.obj["console"]
    
    # Resolve the target directory
    target_dir = Path(path).resolve() / name
    
    if not target_dir.exists():
        logger.error(f"Project directory does not exist: {target_dir}")
        raise SystemExit(1)
    
    # Verify it's a raccoon project
    project_file = target_dir / "raccoon.project.yml"
    if not project_file.exists():
        logger.error(f"Not a raccoon project (no raccoon.project.yml found): {target_dir}")
        raise SystemExit(1)
    
    # Load project info
    try:
        config = load_project_config(target_dir)
        project_name = config.get('name', name)
    except Exception:
        project_name = name
    
    console.print(f"[yellow]⚠ About to delete project:[/yellow]")
    console.print(f"[bold]  Name:[/bold] {project_name}")
    console.print(f"[bold]  Path:[/bold] {target_dir}")
    
    # Confirm deletion
    if not force:
        console.print("\n[red]⚠ WARNING: This will permanently delete the project and cannot be undone![/red]")
        if not click.confirm("Are you sure you want to continue?", default=False):
            console.print("[yellow]Cancelled.[/yellow]")
            return
    
    # Delete the project directory
    try:
        shutil.rmtree(target_dir)
        console.print(f"\n[green]✓ Project '{project_name}' deleted successfully[/green]")
    except Exception as e:
        logger.error(f"Failed to delete project: {e}")
        raise SystemExit(1)

