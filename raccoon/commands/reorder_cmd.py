"""Reorder missions command for raccoon CLI."""

from __future__ import annotations

import logging
import re
from typing import List

import click
from rich.console import Console
from rich.table import Table

from raccoon.project import ProjectError, load_project_config, find_project_root, save_project_keys

logger = logging.getLogger("raccoon")

_MISSION_NUMBER_RE = re.compile(r'^[Mm](\d{3})')


def _mission_class_name(entry) -> str:
    """Extract the class name string from a mission list entry (str or dict)."""
    if isinstance(entry, dict):
        return list(entry.keys())[0]
    return str(entry)


def _normalize_for_match(name: str) -> str:
    """Lowercase, strip 'mission' suffix, strip M-prefix for fuzzy matching."""
    name = name.strip()
    if name.lower().endswith('mission'):
        name = name[:-7]
    m = _MISSION_NUMBER_RE.match(name)
    if m:
        prefix = m.group(0)  # e.g. "M010"
        rest = name[len(prefix):]
        return (prefix + rest).lower().replace('_', '')
    return name.lower().replace('_', '')


def _resolve_order(missions: list, args: tuple[str, ...]) -> list | None:
    """
    Resolve *args* to an ordered list of mission entries.

    Args may be:
    - 1-based integer indices ("1 3 2")
    - Full or partial class names ("M010DriveToSmth", "DriveToSmth")

    Returns the reordered list, or None if resolution fails.
    """
    # Detect index mode: all args are digits
    if all(a.isdigit() for a in args):
        indices = [int(a) for a in args]
        if set(indices) != set(range(1, len(missions) + 1)):
            logger.error(
                f"Indices must be a permutation of 1–{len(missions)}, got: {list(args)}"
            )
            return None
        return [missions[i - 1] for i in indices]

    # Name mode: match each arg against the mission list
    normalized_missions = [_normalize_for_match(_mission_class_name(e)) for e in missions]
    result = []
    used = set()
    for arg in args:
        norm_arg = _normalize_for_match(arg)
        matches = [i for i, nm in enumerate(normalized_missions) if nm == norm_arg]
        if not matches:
            # Try prefix match
            matches = [i for i, nm in enumerate(normalized_missions) if nm.startswith(norm_arg)]
        if len(matches) == 0:
            logger.error(f"No mission matching '{arg}' found.")
            return None
        if len(matches) > 1:
            names = [_mission_class_name(missions[i]) for i in matches]
            logger.error(f"'{arg}' is ambiguous — matches: {names}")
            return None
        idx = matches[0]
        if idx in used:
            logger.error(f"Mission '{_mission_class_name(missions[idx])}' appears more than once.")
            return None
        used.add(idx)
        result.append(missions[idx])

    if len(result) != len(missions):
        missing = [_mission_class_name(missions[i]) for i in range(len(missions)) if i not in used]
        logger.error(f"Not all missions were included. Missing: {missing}")
        return None

    return result


def _print_mission_table(console: Console, missions: list, title: str = "Mission Order") -> None:
    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Mission", style="cyan")
    for idx, entry in enumerate(missions, 1):
        table.add_row(str(idx), _mission_class_name(entry))
    console.print(table)


def _interactive_reorder(console: Console, missions: list) -> list | None:
    """Show the current list and prompt the user for a new order. Returns reordered list or None."""
    console.print()
    _print_mission_table(console, missions, title="Current Mission Order")
    console.print()
    console.print("[dim]Enter the new order as space-separated indices (e.g. [bold]1 3 2[/bold]),[/dim]")
    console.print("[dim]or press Enter to cancel.[/dim]\n")

    raw = click.prompt("New order", default="", show_default=False).strip()
    if not raw:
        console.print("[yellow]Cancelled.[/yellow]")
        return None

    args = tuple(raw.split())
    return _resolve_order(missions, args)


@click.group(name="reorder")
def reorder_command() -> None:
    """Reorder items in the project."""
    pass


@reorder_command.command(name="missions")
@click.argument("order", nargs=-1)
@click.pass_context
def reorder_missions_command(ctx: click.Context, order: tuple[str, ...]) -> None:
    """Reorder missions in the project config.

    Without arguments, launches an interactive prompt showing the current order.

    With arguments, reorders non-interactively:

    \b
    By index (1-based):
        raccoon reorder missions 1 3 2

    By class name (with or without Mission suffix / M-prefix):
        raccoon reorder missions M010Drive M000Setup M020Return
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = find_project_root()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    try:
        config = load_project_config(project_root)
        missions: List = config.get('missions', [])
        if not isinstance(missions, list):
            missions = []
    except ProjectError as exc:
        logger.error(f"Failed to load project config: {exc}")
        raise SystemExit(1) from exc

    if not missions:
        console.print("[yellow]No missions configured in this project.[/yellow]")
        return

    if len(missions) == 1:
        console.print("[yellow]Only one mission — nothing to reorder.[/yellow]")
        return

    if order:
        new_order = _resolve_order(missions, order)
    else:
        new_order = _interactive_reorder(console, missions)

    if new_order is None:
        raise SystemExit(1)

    # Show the proposed new order
    console.print()
    _print_mission_table(console, new_order, title="New Mission Order")

    if not order:
        # In interactive mode, confirm before saving
        console.print()
        if not click.confirm("Save this order?", default=True):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    save_project_keys(project_root, {"missions": new_order})
    console.print("[green]✓ Mission order updated.[/green]")
