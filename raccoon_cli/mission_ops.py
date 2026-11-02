"""Shared mission operations: list, remove, rename and reorder.

The ``raccoon missions`` CLI group and the IDE backend are meant to call these
functions directly (see CLAUDE.md: one code path, thin wrappers). Creation lives
in :func:`raccoon_cli.project_creation.create_mission`.

All functions raise :class:`MissionError` for user-facing problems (unknown or
ambiguous mission, name collisions, ...). Callers translate that into
``SystemExit`` (CLI) or ``HTTPException`` (IDE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from raccoon_cli.mission_config import (
    ensure_mission_list,
    mission_entry_kind,
    mission_entry_name,
    remove_mission_entry,
    rename_mission_entry,
    replace_mission_name,
)
from raccoon_cli.naming import mission_module_name, normalize_name
from raccoon_cli.project import load_project_config, save_project_keys

_NUMBER_PREFIX_RE = re.compile(r"^[Mm](\d+)")
_NUMBER_ONLY_RE = re.compile(r"[Mm](\d+)")

SETUP_NUMBER = 0
SHUTDOWN_NUMBER = 999
NUMBER_STEP = 10


class MissionError(Exception):
    """A mission operation could not be carried out (message is user-facing)."""


@dataclass(frozen=True)
class MissionInfo:
    """One configured mission, as shown by ``raccoon missions list``."""

    index: int  # 1-based position in the config
    class_name: str
    file_name: str
    kind: Optional[str]  # "setup", "shutdown" or None
    file_exists: bool


@dataclass(frozen=True)
class RemoveResult:
    class_name: str
    removed_from_config: bool
    file: Optional[Path]  # file that existed for this mission, if any
    file_deleted: bool


@dataclass(frozen=True)
class RenameResult:
    old_class: str
    new_class: str
    old_file: str
    new_file: str


@dataclass(frozen=True)
class FileMove:
    old_file: str
    new_file: str


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------


def number_prefix(class_name: str) -> Optional[str]:
    """Return the ``M010`` style prefix of a class name, or ``None``."""
    m = _NUMBER_PREFIX_RE.match(class_name)
    return m.group(0) if m else None


def mission_number(class_name: str) -> Optional[int]:
    """Return the numeric value of the ``M010`` prefix (10), or ``None``."""
    prefix = number_prefix(class_name)
    return int(prefix[1:]) if prefix else None


def strip_number_prefix(name: str) -> str:
    prefix = number_prefix(name)
    return name[len(prefix):] if prefix else name


def strip_mission_suffix(name: str) -> str:
    return name[:-7] if name.lower().endswith("mission") else name


def _match_key(name: str) -> str:
    """Case/underscore-insensitive comparison key (suffix stripped)."""
    return strip_mission_suffix(name.strip()).lower().replace("_", "")


def _missions(config: Dict[str, Any]) -> List[Any]:
    missions = config.get("missions", [])
    return missions if isinstance(missions, list) else []


def _load_missions(project_root: Path) -> List[Any]:
    return _missions(load_project_config(project_root))


def _missions_dir(project_root: Path) -> Path:
    return project_root / "src" / "missions"


def _name(entry: Any) -> str:
    return mission_entry_name(entry) or ""


def resolve_mission(missions: Sequence[Any], query: str, *, allow_prefix: bool = False) -> int:
    """Return the index of the mission matching *query*.

    *query* may be just the number (``M010``), or a name that includes or
    omits the ``Mission`` suffix and the ``M010`` prefix. With a number prefix
    it must match the full class name; without one it matches the name part
    only (so ``Drive`` finds ``M010DriveMission``).
    With ``allow_prefix`` a unique name prefix is accepted when there is no
    exact match (used by ``reorder``, never by destructive commands).
    """
    query = query.strip()
    if not query:
        raise MissionError("Mission name must not be empty.")

    only_number = _NUMBER_ONLY_RE.fullmatch(query)
    if only_number:
        wanted_number = int(only_number.group(1))
        matches = [i for i, e in enumerate(missions) if mission_number(_name(e)) == wanted_number]
    else:
        has_number = number_prefix(strip_mission_suffix(query)) is not None

        def key(class_name: str) -> str:
            k = _match_key(class_name)
            return k if has_number else _match_key(strip_number_prefix(class_name))

        wanted = _match_key(query)
        keys = [key(_name(e)) for e in missions]

        matches = [i for i, k in enumerate(keys) if k == wanted]
        if not matches and allow_prefix:
            matches = [i for i, k in enumerate(keys) if k.startswith(wanted)]
    if not matches:
        raise MissionError(f"No mission matching '{query}' found.")
    if len(matches) > 1:
        names = [_name(missions[i]) for i in matches]
        raise MissionError(f"'{query}' is ambiguous - matches: {', '.join(names)}")
    return matches[0]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def list_missions(project_root: Path) -> List[MissionInfo]:
    """All configured missions in config order, with file existence."""
    missions_dir = _missions_dir(project_root)
    result = []
    for idx, entry in enumerate(_load_missions(project_root), 1):
        class_name = _name(entry)
        if not class_name:
            continue
        file_name = f"{mission_module_name(class_name)}.py"
        result.append(
            MissionInfo(
                index=idx,
                class_name=class_name,
                file_name=file_name,
                kind=mission_entry_kind(entry),
                file_exists=(missions_dir / file_name).exists(),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def remove_mission(project_root: Path, query: str, *, keep_file: bool = False) -> RemoveResult:
    """Remove a mission from the config and (unless *keep_file*) delete its file."""
    config = load_project_config(project_root)
    missions = _missions(config)
    class_name = _name(missions[resolve_mission(missions, query)])

    removed = remove_mission_entry(config, class_name)
    if removed:
        save_project_keys(project_root, {"missions": config["missions"]})

    mission_file = _missions_dir(project_root) / f"{mission_module_name(class_name)}.py"
    existed = mission_file.exists()
    deleted = False
    if existed and not keep_file:
        mission_file.unlink()
        deleted = True

    return RemoveResult(
        class_name=class_name,
        removed_from_config=removed,
        file=mission_file if existed else None,
        file_deleted=deleted,
    )


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def rename_mission(project_root: Path, query: str, new_name: str) -> RenameResult:
    """Rename a mission: config entry, file name, class name and references.

    The number prefix (``M010``) is kept; ``new_name`` is just the new name
    part. References are updated in ``src/**/*.py``.
    """
    new_base = strip_mission_suffix(strip_number_prefix(new_name.strip()))
    nn = normalize_name(new_base, strip_suffix="")
    if not nn.pascal:
        raise MissionError("New mission name must not be empty.")

    config = load_project_config(project_root)
    missions = _missions(config)
    old_class = _name(missions[resolve_mission(missions, query)])

    new_class = f"{number_prefix(old_class) or ''}{nn.pascal}Mission"
    if new_class == old_class:
        raise MissionError(f"Mission is already named '{old_class}'.")
    if any(_name(e) == new_class for e in missions):
        raise MissionError(f"A mission named '{new_class}' already exists.")

    missions_dir = _missions_dir(project_root)
    old_file = missions_dir / f"{mission_module_name(old_class)}.py"
    new_file = missions_dir / f"{mission_module_name(new_class)}.py"
    if new_file.exists():
        raise MissionError(f"Mission file already exists: {new_file.name}")

    if old_file.exists():
        old_file.rename(new_file)

    _apply_renames(
        project_root,
        {old_class: new_class, old_file.stem: new_file.stem},
    )

    rename_mission_entry(config, old_class, new_class)
    save_project_keys(project_root, {"missions": config["missions"]})

    return RenameResult(old_class, new_class, old_file.name, new_file.name)


# ---------------------------------------------------------------------------
# Reorder (+ renumber to M010, M020, ...)
# ---------------------------------------------------------------------------


def is_pinned_setup(entry: Any) -> bool:
    return mission_entry_kind(entry) == "setup"


def is_pinned_shutdown(entry: Any) -> bool:
    return mission_entry_kind(entry) == "shutdown"


def resolve_order(missions: Sequence[Any], args: Sequence[str]) -> List[Any]:
    """Resolve CLI *args* to a full ordering of *missions*.

    Args are either 1-based indices (a permutation of ``1..n``) or mission
    names (unique prefixes allowed) covering every mission exactly once.
    """
    if args and all(a.isdigit() for a in args):
        indices = [int(a) for a in args]
        if sorted(indices) != list(range(1, len(missions) + 1)):
            raise MissionError(
                f"Indices must be a permutation of 1-{len(missions)}, got: {list(args)}"
            )
        return [missions[i - 1] for i in indices]

    used: List[int] = []
    for arg in args:
        idx = resolve_mission(missions, arg, allow_prefix=True)
        if idx in used:
            raise MissionError(f"Mission '{_name(missions[idx])}' appears more than once.")
        used.append(idx)

    if len(used) != len(missions):
        missing = [_name(m) for i, m in enumerate(missions) if i not in used]
        raise MissionError(f"Not all missions were included. Missing: {', '.join(missing)}")
    return [missions[i] for i in used]


def reorder_missions(project_root: Path, ordered: Sequence[Any]) -> List[FileMove]:
    """Persist *ordered* and renumber all missions: M000 setup, M010, M020, ..., M999 shutdown.

    Renames the mission files and class names and updates references in
    ``src/**/*.py``. Returns the file moves that happened.
    """
    missions_dir = _missions_dir(project_root)

    plan = []  # (entry, old_class, new_class, old_file, new_file)
    counter = 0
    for entry in ordered:
        old_class = _name(entry)
        if is_pinned_setup(entry):
            number = SETUP_NUMBER
        elif is_pinned_shutdown(entry):
            number = SHUTDOWN_NUMBER
        else:
            counter += 1
            number = counter * NUMBER_STEP
        base = strip_mission_suffix(strip_number_prefix(old_class))
        new_class = f"M{number:03d}{base}Mission"
        plan.append(
            (
                entry,
                old_class,
                new_class,
                missions_dir / f"{mission_module_name(old_class)}.py",
                missions_dir / f"{mission_module_name(new_class)}.py",
            )
        )

    new_names = [p[2] for p in plan]
    if len(set(new_names)) != len(new_names):
        raise MissionError("Renumbering would produce duplicate mission names.")

    # Two phases (old -> temp -> new) so missions can swap numbers safely.
    changed = [p for p in plan if p[1] != p[2]]
    temps: Dict[Path, Path] = {}
    for _, _, _, old_file, new_file in changed:
        if old_file != new_file and old_file.exists():
            tmp = old_file.with_name(f"_tmp_renum_{old_file.name}")
            old_file.rename(tmp)
            temps[old_file] = tmp

    moves: List[FileMove] = []
    mapping: Dict[str, str] = {}
    for _, old_class, new_class, old_file, new_file in changed:
        tmp = temps.get(old_file)
        if tmp is not None:
            tmp.rename(new_file)
            moves.append(FileMove(old_file.name, new_file.name))
        mapping[old_class] = new_class
        mapping[old_file.stem] = new_file.stem
    _apply_renames(project_root, mapping)

    updated = [replace_mission_name(entry, new_class) for entry, _, new_class, _, _ in plan]
    save_project_keys(project_root, {"missions": updated})
    return moves


# ---------------------------------------------------------------------------
# Reference patching
# ---------------------------------------------------------------------------


def _apply_renames(project_root: Path, mapping: Dict[str, str]) -> None:
    """Rewrite identifiers in ``src/**/*.py`` in a single pass.

    One regex over all old names (longest first, whole words only) so chained
    or swapped renames can never apply twice to the same occurrence.
    """
    mapping = {old: new for old, new in mapping.items() if old != new}
    src = project_root / "src"
    if not mapping or not src.is_dir():
        return

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)) + r")\b"
    )
    for path in src.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_text = pattern.sub(lambda m: mapping[m.group(1)], text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
