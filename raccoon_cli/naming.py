"""Helpers for normalizing user-facing mission names into code-safe forms."""

import re
from dataclasses import dataclass

_CAMEL_SPLIT = re.compile(r'(?<!^)(?=[A-Z])')
_SEP_NORMALIZE = re.compile(r'[_\-\s]+')


@dataclass(frozen=True)
class NormalizedName:
    """Canonical spellings of a user-provided mission name."""

    raw: str
    snake: str
    pascal: str  # Correct naming: PascalCase


def normalize_name(s: str, strip_suffix: str = "mission") -> NormalizedName:
    """Normalize a mission name into ``snake_case`` and ``PascalCase`` variants."""
    raw = s.strip()
    if _CAMEL_SPLIT.search(raw) and " " not in raw and "_" not in raw and "-" not in raw:
        tokens = [t.lower() for t in _CAMEL_SPLIT.split(raw)]
    else:
        tmp = _SEP_NORMALIZE.sub(" ", raw)
        tokens = tmp.lower().split()

    if tokens and tokens[-1].lower() == strip_suffix.lower():
        tokens = tokens[:-1]

    snake = "_".join(tokens)
    pascal = "".join(t.capitalize() for t in tokens)
    return NormalizedName(raw=raw, snake=snake, pascal=pascal)


def mission_module_name(class_name: str) -> str:
    """Module (file stem) of a mission class, e.g. ``M010DriveFooMission`` -> ``m010_drive_foo_mission``.

    This is the convention the robot code generator imports missions by.
    """
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", class_name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
