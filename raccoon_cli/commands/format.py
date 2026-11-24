"""Format command — formats the project's source code with Black."""

from __future__ import annotations

import click
from rich.console import Console

from raccoon_cli.formatting import (
    FORMATTING_CONFIG_FILE,
    FormattingConfigError,
    format_project,
    has_formatting_config,
    load_formatting_config,
)
from raccoon_cli.project import ProjectError, find_project_root


@click.command(name="format")
@click.option(
    "--check",
    is_flag=True,
    help="Only report files that would change; write nothing. Exits 1 if any would change.",
)
@click.pass_context
def format_command(ctx: click.Context, check: bool) -> None:
    """Format the Python files in src/ with Black.

    Formatting is on when the project has a raccoon.formatting.yml. Without
    that file nothing is formatted and a warning is shown. Set "auto_format: true"
    in it to format automatically before "raccoon run".
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = find_project_root()
        if not has_formatting_config(project_root):
            console.print(
                f"[yellow]⚠ No {FORMATTING_CONFIG_FILE} in this project, so nothing was formatted.[/yellow]\n"
                f"  [dim]Create {FORMATTING_CONFIG_FILE} (an empty file is enough) to turn formatting on.[/dim]"
            )
            return
        config = load_formatting_config(project_root)
    except ProjectError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise SystemExit(1) from exc
    except FormattingConfigError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise SystemExit(1) from exc

    result = format_project(project_root, check=check, config=config)

    for path in result.changed:
        verb = "would reformat" if check else "reformatted"
        console.print(f"[cyan]{verb}[/cyan] {path}")
    for path, message in result.errors:
        console.print(f"[red]✗ {path}: {message}[/red]")

    summary = f"{len(result.changed)} {'would change' if check else 'changed'}, {result.unchanged} unchanged"
    if result.errors:
        console.print(f"[red]{summary}, {len(result.errors)} failed[/red]")
        raise SystemExit(1)
    if check and result.changed:
        console.print(f"[yellow]{summary}[/yellow]")
        raise SystemExit(1)
    console.print(f"[green]✓ {summary}[/green]")
