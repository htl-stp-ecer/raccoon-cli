"""Reorder missions command for raccoon CLI."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List

import click
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console

from raccoon_cli.naming import normalize_name
from raccoon_cli.project import ProjectError, load_project_config, find_project_root, save_project_keys

logger = logging.getLogger("raccoon")

_MISSION_NUMBER_RE = re.compile(r'^[Mm](\d+)')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mission_class_name(entry) -> str:
    """Extract the class name string from a mission list entry (str or dict)."""
    if isinstance(entry, dict):
        return list(entry.keys())[0]
    return str(entry)


def _strip_m_prefix(class_name: str) -> str:
    m = _MISSION_NUMBER_RE.match(class_name)
    return class_name[len(m.group(0)):] if m else class_name


def _mission_role(entry) -> str | None:
    """Return the role tag ('setup', 'shutdown', …) from a dict entry, or None."""
    if isinstance(entry, dict):
        return list(entry.values())[0] or None
    return None


def _is_pinned_setup(entry) -> bool:
    return _mission_role(entry) == "setup"


def _is_pinned_shutdown(entry) -> bool:
    return _mission_role(entry) == "shutdown"


def _normalize_for_match(name: str) -> str:
    """Lowercase, strip 'mission' suffix, strip M-prefix for fuzzy matching."""
    name = name.strip()
    if name.lower().endswith('mission'):
        name = name[:-7]
    m = _MISSION_NUMBER_RE.match(name)
    if m:
        prefix = m.group(0)
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
    if all(a.isdigit() for a in args):
        indices = [int(a) for a in args]
        if set(indices) != set(range(1, len(missions) + 1)):
            logger.error(
                f"Indices must be a permutation of 1–{len(missions)}, got: {list(args)}"
            )
            return None
        return [missions[i - 1] for i in indices]

    normalized_missions = [_normalize_for_match(_mission_class_name(e)) for e in missions]
    result = []
    used = set()
    for arg in args:
        norm_arg = _normalize_for_match(arg)
        matches = [i for i, nm in enumerate(normalized_missions) if nm == norm_arg]
        if not matches:
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


# ---------------------------------------------------------------------------
# Interactive TUI reorder
# ---------------------------------------------------------------------------

def _interactive_reorder(missions: list) -> list | None:
    """
    TUI list: entries with role 'setup'/'shutdown' are pinned at top/bottom.
    Arrows move cursor, Space grabs/drops, Enter saves, Escape cancels.
    """
    setup_entry    = next((e for e in missions if _is_pinned_setup(e)),    None)
    shutdown_entry = next((e for e in missions if _is_pinned_shutdown(e)), None)

    # Entries the user can reorder
    moveable = [e for e in missions
                if not _is_pinned_setup(e) and not _is_pinned_shutdown(e)]

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
                ("fg:#4b5563", _mission_class_name(setup_entry)),
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
                (name_style, _mission_class_name(entry)),
                ("", "\n"),
            ]
            seq += 1

        if shutdown_entry is not None:
            result += [
                ("", "  "),
                ("fg:#374151", f"{seq:>2}."),
                ("", "  "),
                ("fg:#4b5563", _mission_class_name(shutdown_entry)),
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
# Renumbering (M010, M020, M030, ...)
# ---------------------------------------------------------------------------

def _renumber_all(project_root: Path, ordered_missions: list, console: Console) -> list:
    """
    Renumber all missions to M010, M020, M030 … in the given order.
    Renames files, updates class declarations, and fixes main.py imports.
    Returns the updated config list (new class names).
    """
    missions_dir = project_root / "src" / "missions"
    main_py      = project_root / "src" / "main.py"

    # ── Build rename plan ────────────────────────────────────────────────────
    # Assign fixed numbers for pinned entries; regular missions get 010, 020, …
    regular_counter = 0
    plan = []
    for entry in ordered_missions:
        old_class = _mission_class_name(entry)
        if _is_pinned_setup(entry):
            new_num = 0          # → M000
        elif _is_pinned_shutdown(entry):
            new_num = 999        # → M999
        else:
            regular_counter += 1
            new_num = regular_counter * 10  # M010, M020, …

        m         = _MISSION_NUMBER_RE.match(old_class)
        old_num   = int(m.group(1)) if m else None
        base_suf  = _strip_m_prefix(old_class)                    # e.g. "DriveForwardMission"
        base_pascal = base_suf[:-7] if base_suf.endswith("Mission") else base_suf

        snake     = normalize_name(base_pascal, strip_suffix="").snake
        new_class = f"M{new_num:03d}{base_pascal}Mission"
        # Preserve the original digit width when building the old snake name
        if old_num is not None:
            old_digits = m.group(1)  # exact string from the source, e.g. "01" or "010"
            old_snake = f"m{old_digits}_{snake}"
        else:
            old_snake = None
        new_snake = f"m{new_num:03d}_{snake}"
        old_file  = (missions_dir / f"{old_snake}_mission.py") if old_snake else None
        new_file  = missions_dir / f"{new_snake}_mission.py"

        plan.append({
            "old_class": old_class,
            "new_class": new_class,
            "old_file":  old_file,
            "new_file":  new_file,
            "old_snake": old_snake,
            "new_snake": new_snake,
            "entry":     entry,
        })

    # ── Phase 1: move every file that needs renaming to a temp name ──────────
    # (avoids conflicts when two missions swap numbers)
    temp_map: dict[Path, Path] = {}
    for item in plan:
        old_f = item["old_file"]
        new_f = item["new_file"]
        if old_f and old_f != new_f and old_f.exists():
            tmp = old_f.parent / f"_tmp_renum_{old_f.name}"
            old_f.rename(tmp)
            temp_map[old_f] = tmp

    # ── Phase 2: rename temp → final, patch class name & main.py ────────────
    main_content: str | None = None
    if main_py.exists():
        main_content = main_py.read_text(encoding="utf-8")

    for item in plan:
        old_class = item["old_class"]
        new_class = item["new_class"]
        old_f     = item["old_file"]
        new_f     = item["new_file"]
        old_snake = item["old_snake"]
        new_snake = item["new_snake"]

        if old_class == new_class:
            continue

        # Rename temp → final
        actual = temp_map.get(old_f)
        if actual and actual.exists():
            actual.rename(new_f)
            console.print(f"[dim]  {old_f.name if old_f else '?'} → {new_f.name}[/dim]")

        # Patch class declaration inside the file
        if new_f.exists():
            text = new_f.read_text(encoding="utf-8")
            if f"class {old_class}" in text:
                text = text.replace(f"class {old_class}", f"class {new_class}")
                new_f.write_text(text, encoding="utf-8")

        # Patch main.py (accumulate, write once at the end)
        if main_content is not None and old_snake:
            old_import = f"from .missions.{old_snake}_mission import {old_class}"
            new_import = f"from .missions.{new_snake}_mission import {new_class}"
            main_content = main_content.replace(old_import, new_import)

    if main_content is not None and main_py.exists():
        main_py.write_text(main_content, encoding="utf-8")

    # ── Build updated config list ────────────────────────────────────────────
    result = []
    for item in plan:
        entry     = item["entry"]
        new_class = item["new_class"]
        if isinstance(entry, dict):
            old_val = list(entry.values())[0]
            result.append({new_class: old_val})
        else:
            result.append(new_class)
    return result


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

@click.group(name="reorder")
def reorder_command() -> None:
    """Reorder items in the project."""
    pass


@reorder_command.command(name="missions")
@click.argument("order", nargs=-1)
@click.pass_context
def reorder_missions_command(ctx: click.Context, order: tuple[str, ...]) -> None:
    """Reorder missions in the project config.

    Without arguments, launches an interactive TUI (setup/shutdown are pinned).

    With arguments, reorders non-interactively:

    \b
    By index (1-based):
        raccoon reorder missions 1 3 2

    By class name (with or without Mission suffix / M-prefix):
        raccoon reorder missions M010Drive M000Setup M020Return

    All missions are automatically renumbered M010, M020, M030 … after reordering.
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
        console.print("[yellow]Only one mission — renumbering to M010.[/yellow]")
        updated = _renumber_all(project_root, missions, console)
        save_project_keys(project_root, {"missions": updated})
        console.print("[green]✓ Done.[/green]")
        return

    if order:
        new_order = _resolve_order(missions, order)
        if new_order is None:
            raise SystemExit(1)
    else:
        moveable_count = sum(
            1 for e in missions
            if not _is_pinned_setup(e) and not _is_pinned_shutdown(e)
        )
        if moveable_count <= 1:
            console.print("[yellow]Nothing to reorder (only pinned missions).[/yellow]")
            return

        new_order = _interactive_reorder(missions)
        if new_order is None:
            console.print("[yellow]Cancelled.[/yellow]")
            return

    console.print()
    updated = _renumber_all(project_root, new_order, console)
    save_project_keys(project_root, {"missions": updated})
    console.print("[green]✓ Mission order updated and renumbered.[/green]")
