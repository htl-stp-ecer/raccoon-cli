"""``raccoon missions`` - create, remove, list, rename and reorder missions.

Thin CLI wrappers: argument parsing and output only. The logic lives in
:mod:`raccoon_cli.mission_ops` and :mod:`raccoon_cli.project_creation`, which
the IDE backend uses as well.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import click
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console
from rich.table import Table

from raccoon_cli import mission_ops
from raccoon_cli.mission_config import mission_entry_name
from raccoon_cli.mission_ops import MissionError, is_pinned_setup, is_pinned_shutdown
from raccoon_cli.project import ProjectError, find_project_root, load_project_config
from raccoon_cli.project_creation import create_mission

logger = logging.getLogger("raccoon")


def _project_root() -> Path:
    try:
        return find_project_root()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc


def _fail(exc: Exception) -> "SystemExit":
    logger.error(str(exc))
    return SystemExit(1)


# ---------------------------------------------------------------------------
# Interactive TUI reorder
# ---------------------------------------------------------------------------

def _interactive_reorder(missions: list) -> list | None:
    """
    TUI list: entries with role 'setup'/'shutdown' are pinned at top/bottom.
    Arrows move cursor, Space grabs/drops, Enter saves, Escape cancels.
    """
    setup_entry    = next((e for e in missions if is_pinned_setup(e)),    None)
    shutdown_entry = next((e for e in missions if is_pinned_shutdown(e)), None)

    # Entries the user can reorder
    moveable = [e for e in missions
                if not is_pinned_setup(e) and not is_pinned_shutdown(e)]

    cursor  = 0
    grabbed = False
    cancelled = False

    def get_text():
        result = []
        result.append(("fg:#6b7280 italic",
                        "  up/down move  .  SPACE grab/drop  .  ENTER save  .  ESC cancel\n\n"))

        seq = 1

        if setup_entry is not None:
            result += [
                ("", "  "),
                ("fg:#374151", f"{seq:>2}."),
                ("", "  "),
                ("fg:#4b5563", mission_entry_name(setup_entry)),
                ("fg:#374151 italic", "  [pinned]"),
                ("", "\n"),
            ]
            seq += 1

        for i, entry in enumerate(moveable):
            is_cur = (i == cursor)
            if grabbed and is_cur:
                icon       = "  \u28ff "
                name_style = "fg:#f59e0b bold"
                num_style  = "fg:#f59e0b"
            elif is_cur:
                icon       = "  > "
                name_style = "fg:#8b5cf6 bold"
                num_style  = "fg:#8b5cf6"
            else:
                icon       = "    "
                name_style = "fg:#e5e7eb"
                num_style  = "fg:#6b7280"
            result += [
                ("", icon),
                (num_style, f"{seq:>2}."),
                ("", "  "),
                (name_style, mission_entry_name(entry)),
                ("", "\n"),
            ]
            seq += 1

        if shutdown_entry is not None:
            result += [
                ("", "  "),
                ("fg:#374151", f"{seq:>2}."),
                ("", "  "),
                ("fg:#4b5563", mission_entry_name(shutdown_entry)),
                ("fg:#374151 italic", "  [pinned]"),
                ("", "\n"),
            ]

        return result

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        nonlocal cursor
        if grabbed:
            if cursor > 0:
                moveable[cursor], moveable[cursor - 1] = moveable[cursor - 1], moveable[cursor]
                cursor -= 1
        else:
            if cursor > 0:
                cursor -= 1

    @kb.add("down")
    def _down(event):
        nonlocal cursor
        if grabbed:
            if cursor < len(moveable) - 1:
                moveable[cursor], moveable[cursor + 1] = moveable[cursor + 1], moveable[cursor]
                cursor += 1
        else:
            if cursor < len(moveable) - 1:
                cursor += 1

    @kb.add("space")
    def _space(event):
        nonlocal grabbed
        grabbed = not grabbed

    @kb.add("enter")
    def _enter(event):
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event):
        nonlocal cancelled
        cancelled = True
        event.app.exit()

    layout = Layout(Window(content=FormattedTextControl(get_text, focusable=True)))
    app = Application(layout=layout, key_bindings=kb, full_screen=False, mouse_support=False)
    app.run()

    if cancelled:
        return None

    # Reassemble: setup → moveable → shutdown
    result = []
    if setup_entry is not None:
        result.append(setup_entry)
    result.extend(moveable)
    if shutdown_entry is not None:
        result.append(shutdown_entry)
    return result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group(name="missions")
def missions_group() -> None:
    """Create, remove, list, rename and reorder missions."""


@missions_group.command(name="create")
@click.argument("name")
@click.pass_context
def create_command(ctx: click.Context, name: str) -> None:
    """Create a new mission in the current project.

    NAME must start with the mission number, e.g. M030Wave
    (creates M030WaveMission). M000 and M999 are reserved for setup/shutdown.
    """
    console: Console = ctx.obj["console"]
    project_root = _project_root()

    number = mission_ops.mission_number(name.strip())
    base = mission_ops.strip_number_prefix(name.strip()).lstrip("_- ")
    if number is None or not base:
        raise _fail(
            MissionError(
                f"Mission name must start with its number and have a name, "
                f"e.g. 'M030Wave' (got '{name}')."
            )
        )

    console.print(f"[cyan]Creating mission '{name}'...[/cyan]")
    try:
        mission_class = create_mission(project_root, base, number=number)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        raise _fail(exc) from exc

    console.print(f"[green]\u2713 Mission '{mission_class}' created successfully[/green]")
    console.print("[cyan]  Added to raccoon.project.yml[/cyan]")


@missions_group.command(name="remove")
@click.argument("name")
@click.option("--keep-file", is_flag=True, help="Only remove it from the config, keep the mission file.")
@click.pass_context
def remove_command(ctx: click.Context, name: str, keep_file: bool) -> None:
    """Remove a mission from the current project.

    NAME is the mission name or just its number (e.g. Drive, M010Drive or M010).
    """
    console: Console = ctx.obj["console"]
    project_root = _project_root()

    try:
        result = mission_ops.remove_mission(project_root, name, keep_file=keep_file)
    except MissionError as exc:
        raise _fail(exc) from exc

    if result.removed_from_config:
        console.print(f"[green]\u2713 Removed '{result.class_name}' from raccoon.project.yml[/green]")
    if result.file is None:
        logger.warning(f"Mission file not found for '{result.class_name}'")
    elif result.file_deleted:
        console.print(f"[green]\u2713 Deleted {result.file.relative_to(project_root)}[/green]")
    else:
        console.print(f"[yellow]Mission file kept: {result.file.relative_to(project_root)}[/yellow]")


@missions_group.command(name="list")
@click.pass_context
def list_command(ctx: click.Context) -> None:
    """List the missions of the current project."""
    console: Console = ctx.obj["console"]
    project_root = _project_root()

    try:
        project_name = load_project_config(project_root).get("name", "Raccoon Project")
        missions = mission_ops.list_missions(project_root)
    except ProjectError as exc:
        raise _fail(exc) from exc

    console.print(f"\n[bold cyan]Project:[/bold cyan] {project_name}")
    console.print(f"[bold cyan]Location:[/bold cyan] {project_root}\n")

    if not missions:
        console.print("[yellow]No missions configured in this project.[/yellow]")
        console.print("[dim]Use 'raccoon missions create <name>' to add one.[/dim]")
        return

    table = Table(title="Missions", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Mission Class", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("File", style="green")
    table.add_column("Status")
    for m in missions:
        status = "[green]\u2713 Exists[/green]" if m.file_exists else "[red]\u2717 Missing[/red]"
        table.add_row(str(m.index), m.class_name, m.kind or "", m.file_name, status)

    console.print(table)
    console.print(f"\n[dim]Total: {len(missions)} mission(s)[/dim]\n")


@missions_group.command(name="rename")
@click.argument("name")
@click.argument("new_name")
@click.pass_context
def rename_command(ctx: click.Context, name: str, new_name: str) -> None:
    """Rename a mission to NEW_NAME (the M-number is kept).

    NAME is the mission name or just its number (e.g. Drive, M010Drive or M010).
    """
    console: Console = ctx.obj["console"]
    project_root = _project_root()

    try:
        result = mission_ops.rename_mission(project_root, name, new_name)
    except MissionError as exc:
        raise _fail(exc) from exc

    console.print(f"[green]\u2713 Renamed '{result.old_class}' \u2192 '{result.new_class}'[/green]")
    console.print(f"[dim]  {result.old_file} \u2192 {result.new_file}[/dim]")


@missions_group.command(name="reorder")
@click.argument("order", nargs=-1)
@click.pass_context
def reorder_command(ctx: click.Context, order: tuple[str, ...]) -> None:
    """Reorder the missions of the current project.

    Without arguments an interactive list opens (setup/shutdown stay pinned).
    With arguments the new order is given directly:

    \b
    By index (1-based):
        raccoon missions reorder 1 3 2
    By name (M-prefix / Mission suffix optional, unique prefixes allowed):
        raccoon missions reorder Setup Drive Collect Shutdown

    All missions are renumbered M010, M020, M030 ... afterwards.
    """
    console: Console = ctx.obj["console"]
    project_root = _project_root()

    try:
        missions: List = load_project_config(project_root).get("missions", [])
    except ProjectError as exc:
        raise _fail(exc) from exc
    if not isinstance(missions, list) or not missions:
        console.print("[yellow]No missions configured in this project.[/yellow]")
        return

    try:
        if order:
            new_order = mission_ops.resolve_order(missions, order)
        elif len(missions) == 1:
            console.print("[yellow]Only one mission \u2014 renumbering to M010.[/yellow]")
            new_order = list(missions)
        else:
            moveable = [e for e in missions if not is_pinned_setup(e) and not is_pinned_shutdown(e)]
            if len(moveable) <= 1:
                console.print("[yellow]Nothing to reorder (only pinned missions).[/yellow]")
                return
            new_order = _interactive_reorder(missions)
            if new_order is None:
                console.print("[yellow]Cancelled.[/yellow]")
                return

        console.print()
        moves = mission_ops.reorder_missions(project_root, new_order)
    except MissionError as exc:
        raise _fail(exc) from exc

    for move in moves:
        console.print(f"[dim]  {move.old_file} \u2192 {move.new_file}[/dim]")
    console.print("[green]\u2713 Mission order updated and renumbered.[/green]")
