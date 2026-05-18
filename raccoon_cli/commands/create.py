"""Create project and mission commands."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click
from rich.console import Console

from raccoon_cli.project import ProjectError, find_project_root
from raccoon_cli.project_creation import create_mission, scaffold_project

logger = logging.getLogger("raccoon")













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

    target_dir = Path(path).resolve() / name

    console.print(f"[cyan]Creating new project '{name}' at {target_dir}...[/cyan]")

    try:
        project_uuid, git_result = scaffold_project(name, target_dir)
    except FileExistsError:
        logger.error(f"Directory {target_dir} already exists")
        raise SystemExit(1)
    except RuntimeError as exc:
        logger.error(str(exc))
        console.print("[yellow]Make sure you have an internet connection and git is installed.[/yellow]")
        raise SystemExit(1) from exc

    console.print(f"[green]✓ Project '{name}' scaffolded at {target_dir}[/green]")
    console.print(f"[cyan]Project UUID: {project_uuid}[/cyan]")

    if git_result.commit_created:
        console.print("[green]✓ Local git history initialized[/green]")
        console.print(f"[dim]Initial snapshot: {git_result.commit_sha}[/dim]")
    elif git_result.reason == "git_unavailable":
        console.print("[yellow]Git is not installed. Skipping local history initialization.[/yellow]")
    elif git_result.reason not in {"already_git_repo", "no_changes"}:
        console.print(f"[yellow]Warning: Could not initialize local git history ({git_result.error})[/yellow]")

    if not no_wizard:
        console.print("\n[cyan]Launching setup wizard to finalize project configuration...[/cyan]\n")

        from raccoon_cli.commands.wizard import wizard_command

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

    try:
        project_root = find_project_root()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    console.print(f"[cyan]Creating mission '{name}'...[/cyan]")

    try:
        mission_class = create_mission(project_root, name)
    except FileExistsError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except FileNotFoundError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    console.print(f"[green]✓ Mission '{mission_class}' created successfully[/green]")
    console.print(f"[cyan]  Added to raccoon.project.yml[/cyan]")
