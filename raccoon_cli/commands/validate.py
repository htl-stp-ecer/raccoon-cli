"""Validate command — checks config/file/import consistency."""

from __future__ import annotations

import click
from rich.console import Console

from raccoon_cli.project import ProjectError, find_project_root
from raccoon_cli.validation import Severity, validate_project


@click.command(name="validate")
@click.option(
    "--no-python-compile",
    is_flag=True,
    help="Skip Python bytecode compile checks for project source files.",
)
@click.pass_context
def validate_command(ctx: click.Context, no_python_compile: bool) -> None:
    """Check that config, mission files, and imports are consistent."""
    console: Console = ctx.obj["console"]

    try:
        project_root = find_project_root()
    except ProjectError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise SystemExit(1) from exc

    result = validate_project(project_root, python_compile=not no_python_compile)

    if not result.issues:
        console.print("[green]✓ Project is consistent — no issues found.[/green]")
        return

    for issue in result.issues:
        if issue.severity == Severity.ERROR:
            console.print(f"[red]✗ {issue.message}[/red]")
        else:
            console.print(f"[yellow]⚠ {issue.message}[/yellow]")
        if issue.hint:
            console.print(f"  [dim]{issue.hint}[/dim]")

    if result.has_errors:
        console.print()
        n = len(result.errors)
        console.print(f"[red]{n} error(s) found. Fix them before proceeding.[/red]")
        raise SystemExit(1)

    console.print()
    console.print(f"[yellow]{len(result.warnings)} warning(s). No blocking errors.[/yellow]")
