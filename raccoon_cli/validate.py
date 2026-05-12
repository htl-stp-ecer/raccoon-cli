"""Project consistency validation — checks config/file/import alignment."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from raccoon_cli.mission_config import ensure_mission_list, mission_entry_name
from raccoon_cli.naming import normalize_name


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    hint: Optional[str] = None

    def __str__(self) -> str:
        tag = "ERROR" if self.severity == Severity.ERROR else "WARN "
        return f"[{tag}] {self.message}"


@dataclass
class ValidationResult:
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)


# ---------------------------------------------------------------------------
# Name conversion helpers
# ---------------------------------------------------------------------------

_CLASS_RE = re.compile(r'^[Mm](\d{3})(.+?)(?:Mission)?$')
_FILE_STEM_RE = re.compile(r'^m(\d{3})_(.+)_mission$')
_MAIN_IMPORT_RE = re.compile(r'from \.missions\.(\w+_mission) import (\w+Mission)')


def class_name_to_expected_file(class_name: str) -> Optional[str]:
    """Convert a mission class name to its expected filename.

    ``M030HelloMission`` → ``m030_hello_mission.py``
    """
    m = _CLASS_RE.match(class_name)
    if not m:
        return None
    num, rest = m.group(1), m.group(2)
    nn = normalize_name(rest, strip_suffix="")
    return f"m{num}_{nn.snake}_mission.py"


def file_name_to_expected_class(file_name: str) -> Optional[str]:
    """Convert a mission filename to its expected class name.

    ``m030_hello_mission.py`` → ``M030HelloMission``
    """
    stem = file_name[:-3] if file_name.endswith(".py") else file_name
    m = _FILE_STEM_RE.match(stem)
    if not m:
        return None
    num, rest = m.group(1), m.group(2)
    nn = normalize_name(rest, strip_suffix="")
    return f"M{num}{nn.pascal}Mission"


# ---------------------------------------------------------------------------
# Validation checks
# ---------------------------------------------------------------------------

def _check_config_vs_files(
    result: ValidationResult,
    config_classes: set[str],
    missions_dir: Path,
) -> None:
    """ERROR when a config entry has no matching file on disk."""
    for class_name in config_classes:
        expected = class_name_to_expected_file(class_name)
        if expected is None:
            result.add(ValidationIssue(
                severity=Severity.WARNING,
                code="unparseable_mission_name",
                message=f"Cannot derive filename from mission name '{class_name}'",
                hint="Check that the mission follows the MNNNNameMission convention",
            ))
            continue
        if not (missions_dir / expected).exists():
            result.add(ValidationIssue(
                severity=Severity.ERROR,
                code="config_missing_file",
                message=f"Mission '{class_name}' is in config but '{expected}' does not exist",
                hint=f"Create the file or remove '{class_name}' from raccoon.project.yml",
            ))


def _check_files_vs_config(
    result: ValidationResult,
    config_classes: set[str],
    missions_dir: Path,
) -> None:
    """WARNING when a mission file on disk is not registered in config."""
    if not missions_dir.exists():
        return
    for f in sorted(missions_dir.glob("m???_*_mission.py")):
        expected_class = file_name_to_expected_class(f.name)
        if expected_class is None:
            continue
        if expected_class not in config_classes:
            result.add(ValidationIssue(
                severity=Severity.WARNING,
                code="file_not_in_config",
                message=f"'{f.name}' exists but '{expected_class}' is not in config",
                hint=f"Add '{expected_class}' to raccoon.project.yml or delete the file",
            ))


def _check_main_imports(
    result: ValidationResult,
    config_classes: set[str],
    missions_dir: Path,
    main_py: Path,
) -> None:
    """Check main.py imports against files and config."""
    if not main_py.exists():
        return
    content = main_py.read_text(encoding="utf-8")
    for m in _MAIN_IMPORT_RE.finditer(content):
        module_name = m.group(1)   # e.g. m030_hello_mission
        class_name = m.group(2)    # e.g. M030HelloMission
        file_name = f"{module_name}.py"

        if not (missions_dir / file_name).exists():
            result.add(ValidationIssue(
                severity=Severity.ERROR,
                code="import_missing_file",
                message=f"main.py imports '{module_name}' but '{file_name}' does not exist",
                hint="Remove the import or create the mission file",
            ))

        if class_name not in config_classes:
            result.add(ValidationIssue(
                severity=Severity.WARNING,
                code="import_not_in_config",
                message=f"main.py imports '{class_name}' but it is not in config",
                hint=f"Add '{class_name}' to raccoon.project.yml or remove the import",
            ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_project(project_root: Path) -> ValidationResult:
    """Run all project consistency checks and return a structured result."""
    result = ValidationResult()

    try:
        from raccoon_cli.project import load_project_config
        config = load_project_config(project_root)
    except Exception as exc:
        result.add(ValidationIssue(
            severity=Severity.ERROR,
            code="config_load_failed",
            message=f"Could not load project config: {exc}",
        ))
        return result

    missions_dir = project_root / "src" / "missions"
    main_py = project_root / "src" / "main.py"

    mission_list = ensure_mission_list(config)
    config_classes: set[str] = set()
    for entry in mission_list:
        name = mission_entry_name(entry)
        if name:
            config_classes.add(name)

    _check_config_vs_files(result, config_classes, missions_dir)
    _check_files_vs_config(result, config_classes, missions_dir)
    _check_main_imports(result, config_classes, missions_dir, main_py)

    return result
