"""Project consistency validation — config/file/import drift + basic integrity."""

from __future__ import annotations

import ast
import difflib
import logging
import py_compile
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Set

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
# Defs attribute-access check
#
# Missions do ``from src.hardware.defs import Defs`` and reference hardware as
# ``Defs.motor_left`` etc.  Accessing an attribute that the generated ``Defs``
# class does not have raises ``AttributeError`` at runtime — something a plain
# ``py_compile`` syntax check never catches.  We resolve the set of *valid*
# Defs attributes statically (from ``definitions:`` in the config AND from the
# generated ``defs.py`` / ``defs.pyi``) and flag any access outside that set.
# ---------------------------------------------------------------------------

# Names a Defs reference may be bound to (the class and the module singleton).
_DEFS_ACCESS_NAMES = frozenset({"Defs", "defs"})

# Attributes the generator always emits regardless of config content.
_ALWAYS_DEFS_ATTRS = frozenset({"imu", "analog_sensors"})

_SENSOR_LEFT_RE = re.compile(r"^(?P<prefix>.+)_left_(?P<suffix>.+)$")
_SENSOR_RIGHT_RE = re.compile(r"^(?P<prefix>.+)_right_(?P<suffix>.+)$")


def _parse_defs_class_attributes(path: Path) -> Optional[Set[str]]:
    """Return the attribute names declared on ``class Defs`` in a .py/.pyi file.

    Returns None if the file cannot be parsed (a syntax error there is reported
    separately by the compile check).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, ValueError):
        return None

    attrs: Set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Defs":
            for stmt in node.body:
                # defs.py: ``name = <expr>``
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            attrs.add(target.id)
                # defs.pyi stub: ``name: Type``
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    attrs.add(stmt.target.id)
    return attrs


def _defs_names_from_config(definitions: dict) -> Set[str]:
    """Derive the Defs attribute names that codegen *will* produce from config.

    Mirrors ``DefsGenerator``: every definition key becomes an attribute, plus
    the always-present extras, auto-created left/right sensor-pair groups, and
    the wait_for_light companion attributes.  Intentionally over-approximates
    (accepts a few extra names) so it never produces a false positive.
    """
    names: Set[str] = set(_ALWAYS_DEFS_ATTRS)
    lefts: dict[tuple[str, str], str] = {}
    rights: dict[tuple[str, str], str] = {}
    has_wfl = False

    for field_name, hw_cfg in definitions.items():
        if not isinstance(field_name, str):
            continue
        names.add(field_name)

        if field_name == "wait_for_light_sensor":
            has_wfl = True
            if isinstance(hw_cfg, dict) and "drop_fraction" in hw_cfg:
                names.add("wait_for_light_drop_fraction")

        m_left = _SENSOR_LEFT_RE.match(field_name)
        if m_left:
            lefts[(m_left.group("prefix"), m_left.group("suffix"))] = field_name
        m_right = _SENSOR_RIGHT_RE.match(field_name)
        if m_right:
            rights[(m_right.group("prefix"), m_right.group("suffix"))] = field_name

    if has_wfl:
        names.add("wait_for_light_mode")

    for key in lefts:
        if key in rights and key[0].isidentifier():
            names.add(key[0])  # ``<prefix>_left_x`` + ``<prefix>_right_x`` → ``<prefix>``

    return names


def _collect_valid_defs_attributes(
    config: dict,
    project_root: Path,
) -> Optional[Set[str]]:
    """Union of valid Defs attributes from config and generated hardware files.

    Returns None when neither source is available, in which case the
    attribute-access check is skipped rather than guessed.
    """
    names: Set[str] = set()
    found_source = False

    hardware_dir = project_root / "src" / "hardware"
    for fname in ("defs.py", "defs.pyi"):
        parsed = _parse_defs_class_attributes(hardware_dir / fname)
        if parsed is not None and (hardware_dir / fname).exists():
            names |= parsed
            found_source = True

    definitions = config.get("definitions")
    if isinstance(definitions, dict):
        names |= _defs_names_from_config(definitions)
        found_source = True

    if not found_source:
        return None

    names |= _ALWAYS_DEFS_ATTRS
    return names


def _file_imports_defs(tree: ast.AST) -> bool:
    """True if the module imports ``Defs`` or ``defs`` from a ``*.defs`` module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[-1] == "defs":
                if any(alias.name in _DEFS_ACCESS_NAMES for alias in node.names):
                    return True
    return False


def _check_defs_attribute_access(
    result: ValidationResult,
    project_root: Path,
    valid_names: Set[str],
) -> None:
    """ERROR for any ``Defs.<attr>`` access where ``<attr>`` is not a real
    hardware object — this would raise ``AttributeError`` at runtime."""
    src_root = project_root / "src"
    if not src_root.exists():
        return

    for py_file in sorted(src_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        # The generated hardware files define Defs; don't scan them.
        rel_parts = py_file.relative_to(src_root).parts
        if rel_parts[:1] == ("hardware",):
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, ValueError):
            continue  # syntax errors are surfaced by the compile check

        if not _file_imports_defs(tree):
            continue

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in _DEFS_ACCESS_NAMES
            ):
                continue

            attr = node.attr
            if attr.startswith("_"):
                continue  # dunder / private access — not a hardware object
            if attr in valid_names:
                continue

            base = node.value.id
            rel = py_file.relative_to(project_root)
            suggestion = difflib.get_close_matches(attr, valid_names, n=1)
            if suggestion:
                hint = f"Did you mean '{base}.{suggestion[0]}'?"
            else:
                hint = (
                    f"Add '{attr}' under 'definitions:' in raccoon.project.yml "
                    "and run 'raccoon codegen'."
                )
            result.add(ValidationIssue(
                severity=Severity.ERROR,
                code="defs_unknown_attribute",
                message=(
                    f"{rel}:{node.lineno} accesses '{base}.{attr}' "
                    "which does not exist on Defs"
                ),
                hint=hint,
            ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_project(
    project_root: Path,
    *,
    python_compile: bool = True,
    defs_check: bool = True,
) -> ValidationResult:
    """Run all project consistency checks and return a structured result.

    Checks performed:
    - Required config keys (name, uuid)
    - Config entries → mission files on disk
    - Mission files on disk → config entries
    - main.py imports → files + config
    - Python compile check for src/**/*.py (optional)
    - Defs attribute access — flags ``Defs.<attr>`` references that would
      raise ``AttributeError`` at runtime (optional)
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

    if defs_check:
        valid_defs = _collect_valid_defs_attributes(config, project_root)
        if valid_defs is not None:
            _check_defs_attribute_access(result, project_root, valid_defs)

    return result


def run_validation_or_exit(
    console,
    project_root: Path,
    *,
    python_compile: bool = True,
    defs_check: bool = True,
    # Accepted for API compatibility but intentionally ignored:
    # codegen_probe belongs in `raccoon codegen --dry-run`, not here.
    config=None,
    codegen_probe: bool = False,
) -> None:
    """Run validation and abort with SystemExit(1) if any errors are found."""
    result = validate_project(
        project_root, python_compile=python_compile, defs_check=defs_check
    )

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
