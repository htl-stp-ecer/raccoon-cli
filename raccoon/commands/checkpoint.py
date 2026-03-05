"""Checkpoint commands - manage invisible git checkpoints."""

from __future__ import annotations

from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from raccoon.checkpoint import (
    clean_checkpoints,
    delete_checkpoint,
    list_checkpoints,
    restore_checkpoint,
    show_checkpoint_diff,
)
from raccoon.project import find_project_root


def _require_project(console: Console):
    project_root = find_project_root()
    if not project_root:
        console.print("[red]Error: Not in a Raccoon project directory[/red]")
        raise SystemExit(1)
    return project_root


@click.group(name="checkpoint")
def checkpoint_group() -> None:
    """Manage invisible git checkpoints."""


@checkpoint_group.command(name="list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """List all saved checkpoints."""
    console: Console = ctx.obj.get("console", Console())
    project_root = _require_project(console)

    checkpoints = list_checkpoints(project_root)
    if not checkpoints:
        console.print("[dim]No checkpoints found.[/dim]")
        return

    table = Table(show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("SHA", style="cyan", width=9)
    table.add_column("Label")
    table.add_column("Created", style="dim")

    for i, cp in enumerate(checkpoints, 1):
        ts = datetime.fromtimestamp(cp.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(str(i), cp.short_sha, cp.label, ts)

    console.print(table)


@checkpoint_group.command(name="show")
@click.argument("identifier")
@click.pass_context
def show_cmd(ctx: click.Context, identifier: str) -> None:
    """Show the diff of a checkpoint.

    IDENTIFIER is either the index number from 'list' or a short SHA.
    """
    console: Console = ctx.obj.get("console", Console())
    project_root = _require_project(console)

    diff, error = show_checkpoint_diff(project_root, identifier)
    if error:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(1)

    console.print(diff)


@checkpoint_group.command(name="restore")
@click.argument("identifier")
@click.pass_context
def restore_cmd(ctx: click.Context, identifier: str) -> None:
    """Apply a checkpoint to the working tree.

    IDENTIFIER is either the index number from 'list' or a short SHA.
    """
    console: Console = ctx.obj.get("console", Console())
    project_root = _require_project(console)

    success, error = restore_checkpoint(project_root, identifier)
    if not success:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(1)

    console.print("[green]Checkpoint restored to working tree.[/green]")


@checkpoint_group.command(name="delete")
@click.argument("identifier")
@click.pass_context
def delete_cmd(ctx: click.Context, identifier: str) -> None:
    """Delete a single checkpoint.

    IDENTIFIER is either the index number from 'list' or a short SHA.
    """
    console: Console = ctx.obj.get("console", Console())
    project_root = _require_project(console)

    success, error = delete_checkpoint(project_root, identifier)
    if not success:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(1)

    console.print("[green]Checkpoint deleted.[/green]")


@checkpoint_group.command(name="clean")
@click.option("--all", "delete_all", is_flag=True, help="Delete all checkpoints")
@click.option(
    "--older-than",
    "max_age_days",
    type=int,
    default=7,
    show_default=True,
    help="Delete checkpoints older than this many days",
)
@click.pass_context
def clean_cmd(ctx: click.Context, delete_all: bool, max_age_days: int) -> None:
    """Prune old checkpoints."""
    console: Console = ctx.obj.get("console", Console())
    project_root = _require_project(console)

    count, error = clean_checkpoints(project_root, max_age_days=max_age_days, delete_all=delete_all)
    if error:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(1)

    if count == 0:
        console.print("[dim]No checkpoints to clean.[/dim]")
    else:
        console.print(f"[green]Deleted {count} checkpoint(s).[/green]")
