"""raccoon migrate — apply numbered project migrations."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from raccoon_cli.project import ProjectError, require_project, load_project_config

_MIGRATIONS_PKG = "raccoon_cli.migrations"
_FORMAT_VERSION_KEY = "format_version"


def _load_migrations() -> list:
    """Return all migration modules sorted by NUMBER."""
    import raccoon_cli.migrations as pkg
    migrations = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{_MIGRATIONS_PKG}.{info.name}")
        if hasattr(mod, "NUMBER") and hasattr(mod, "run"):
            migrations.append(mod)
    return sorted(migrations, key=lambda m: m.NUMBER)


def _get_format_version(project_root: Path) -> int:
    """Read format_version from raccoon.project.yml. Returns 0 if absent."""
    yml = project_root / "raccoon.project.yml"
    if not yml.exists():
        return 0
    try:
        config = load_project_config(project_root)
        if isinstance(config, dict):
            return int(config.get(_FORMAT_VERSION_KEY, 0))
    except Exception:
        pass
    return 0


def _set_format_version(project_root: Path, version: int) -> None:
    """Write format_version into raccoon.project.yml."""
    yml = project_root / "raccoon.project.yml"
    if not yml.exists():
        return
    content = yml.read_text()
    import re
    if re.search(rf"^{_FORMAT_VERSION_KEY}:", content, re.MULTILINE):
        content = re.sub(
            rf"^{_FORMAT_VERSION_KEY}:.*$",
            f"{_FORMAT_VERSION_KEY}: {version}",
            content,
            flags=re.MULTILINE,
        )
    else:
        content = content.rstrip() + f"\n{_FORMAT_VERSION_KEY}: {version}\n"
    yml.write_text(content)


@click.command(name="migrate")
@click.option("--target", "-t", type=int, default=None, help="Migrate to this version (default: latest)")
@click.option("--dry-run", is_flag=True, help="Show pending migrations without applying them")
@click.pass_context
def migrate_command(ctx: click.Context, target: Optional[int], dry_run: bool) -> None:
    """Apply pending project migrations.

    Reads format_version from raccoon.project.yml and applies all
    migration scripts with a higher NUMBER in order.
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
    except ProjectError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    migrations = _load_migrations()
    if not migrations:
        console.print("[yellow]No migration scripts found.[/yellow]")
        return

    current = _get_format_version(project_root)
    latest = migrations[-1].NUMBER
    apply_up_to = target if target is not None else latest

    pending = [m for m in migrations if current < m.NUMBER <= apply_up_to]

    if not pending:
        console.print(
            f"[green]Already up to date[/green] "
            f"(format_version={current}, latest={latest})"
        )
        return

    table = Table(title="Pending migrations", show_header=True, header_style="bold cyan")
    table.add_column("Nr.", style="dim", width=6)
    table.add_column("Description")
    for m in pending:
        table.add_row(str(m.NUMBER).zfill(4), m.DESCRIPTION)
    console.print(table)

    if dry_run:
        console.print("[yellow]Dry run — no changes applied.[/yellow]")
        return

    for m in pending:
        console.print(f"[cyan]Applying migration {m.NUMBER:04d}: {m.DESCRIPTION}[/cyan]")
        try:
            m.run(project_root)
            _set_format_version(project_root, m.NUMBER)
            console.print(f"[green]  ✓ Migration {m.NUMBER:04d} applied[/green]")
        except Exception as exc:
            console.print(f"[red]  ✗ Migration {m.NUMBER:04d} failed: {exc}[/red]")
            raise SystemExit(1)

    console.print(
        f"\n[bold green]Project is now at format_version={apply_up_to}[/bold green]"
    )
