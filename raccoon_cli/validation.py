"""Project consistency validation — config/file/import drift + basic integrity."""

from __future__ import annotations

import logging
import py_compile
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from raccoon_cli.mission_config import ensure_mission_list, mission_entry_name
from raccoon_cli.naming import normalize_name

logger = logging.getLogger("raccoon")


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
    """``M030HelloMission`` → ``m030_hello_mission.py``"""
    m = _CLASS_RE.match(class_name)
    if not m:
        return None
    num, rest = m.group(1), m.group(2)
    nn = normalize_name(rest, strip_suffix="")
    return f"m{num}_{nn.snake}_mission.py"


def file_name_to_expected_class(file_name: str) -> Optional[str]:
    """``m030_hello_mission.py`` → ``M030HelloMission``"""
    stem = file_name[:-3] if file_name.endswith(".py") else file_name
    m = _FILE_STEM_RE.match(stem)
    if not m:
        return None
    num, rest = m.group(1), m.group(2)
    nn = normalize_name(rest, strip_suffix="")
    return f"M{num}{nn.pascal}Mission"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_required_keys(result: ValidationResult, config: dict) -> None:
    """ERROR when mandatory top-level keys are missing or blank."""
    for key in ("name", "uuid"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            result.add(ValidationIssue(
                severity=Severity.ERROR,
                code="missing_required_key",
                message=f"Missing required project key: '{key}'",
                hint=f"Add '{key}' to raccoon.project.yml",
            ))


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
    """Check main.py imports against files on disk and config."""
    if not main_py.exists():
        return
    content = main_py.read_text(encoding="utf-8")
    for m in _MAIN_IMPORT_RE.finditer(content):
        module_name = m.group(1)
        class_name = m.group(2)
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


def _check_python_compile(
    result: ValidationResult,
    project_root: Path,
) -> None:
    """ERROR for any .py file under src/ that fails to compile."""
    src_root = project_root / "src"
    if not src_root.exists():
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="no_src_dir",
            message="No src/ directory found; skipping Python compile checks",
        ))
        return

    for py_file in sorted(src_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as exc:
            rel = py_file.relative_to(project_root)
            result.add(ValidationIssue(
                severity=Severity.ERROR,
                code="python_compile_error",
                message=f"Syntax error in {rel}: {exc.msg}",
                hint="Fix the syntax error before running",
            ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_project(
    project_root: Path,
    *,
    python_compile: bool = True,
) -> ValidationResult:
    """Run all project consistency checks and return a structured result.

    Checks performed:
    - Required config keys (name, uuid)
    - Config entries → mission files on disk
    - Mission files on disk → config entries
    - main.py imports → files + config
    - Python compile check for src/**/*.py (optional)
    """
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

    _check_required_keys(result, config)

    missions_dir = project_root / "src" / "missions"
    main_py = project_root / "src" / "main.py"

    config_classes: set[str] = set()
    for entry in ensure_mission_list(config):
        name = mission_entry_name(entry)
        if name:
            config_classes.add(name)

    _check_config_vs_files(result, config_classes, missions_dir)
    _check_files_vs_config(result, config_classes, missions_dir)
    _check_main_imports(result, config_classes, missions_dir, main_py)

    if python_compile:
        _check_python_compile(result, project_root)

    return result


def run_validation_or_exit(
    console,
    project_root: Path,
    *,
    python_compile: bool = True,
    # Accepted for API compatibility but intentionally ignored:
    # codegen_probe belongs in `raccoon codegen --dry-run`, not here.
    config=None,
    codegen_probe: bool = False,
) -> None:
    """Run validation and abort with SystemExit(1) if any errors are found."""
    result = validate_project(project_root, python_compile=python_compile)

    for issue in result.warnings:
        console.print(f"[yellow]⚠ validate: {issue.message}[/yellow]")
        if issue.hint:
            console.print(f"  [dim]{issue.hint}[/dim]")

    if result.has_errors:
        console.print()
        for issue in result.errors:
            console.print(f"[red]✗ validate: {issue.message}[/red]")
            if issue.hint:
                console.print(f"  [dim]{issue.hint}[/dim]")
        console.print()
        console.print("[red]Project validation failed. Run 'raccoon validate' for details.[/red]")
        raise SystemExit(1)
