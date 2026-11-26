"""Project consistency validation — config/file/import drift + basic integrity."""

from __future__ import annotations

import ast
import contextlib
import difflib
import importlib.util
import io
import logging
import py_compile
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
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
        return _defs_class_attributes_from_source(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _defs_class_attributes_from_source(source: str) -> Optional[Set[str]]:
    """Return the attribute names declared on ``class Defs`` in *source*."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
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
    generated: Optional[Set[str]] = None,
) -> Optional[Set[str]]:
    """Return the valid Defs attributes, or None if they cannot be determined.

    Sources, in order of preference:

    1. ``generated`` — the names in the ``Defs`` class that the generators
       produce from the *current* config (exact; see ``_check_generation``).
    2. ``definitions:`` in the config, mapped by ``_defs_names_from_config``.
    3. The ``defs.py`` / ``defs.pyi`` already on disk, only when the config has
       no ``definitions:`` at all.

    Files on disk are deliberately NOT merged with the config: a stale
    ``defs.py`` still declaring a removed entry would otherwise hide a real
    error until the next codegen.

    Returning None skips the attribute-access check rather than guessing.
    """
    if generated is not None:
        return set(generated) | _ALWAYS_DEFS_ATTRS

    definitions = config.get("definitions")
    if isinstance(definitions, dict):
        return _defs_names_from_config(definitions) | _ALWAYS_DEFS_ATTRS

    names: Set[str] = set()
    found_source = False
    hardware_dir = project_root / "src" / "hardware"
    for fname in ("defs.py", "defs.pyi"):
        parsed = _parse_defs_class_attributes(hardware_dir / fname)
        if parsed is not None:
            names |= parsed
            found_source = True

    if not found_source:
        return None
    return names | _ALWAYS_DEFS_ATTRS


# ---------------------------------------------------------------------------
# Generation check
#
# Run the real generators in memory (no files written, no black) so validate
# reports exactly what ``raccoon codegen`` would fail on, and so the valid
# ``Defs`` names come from the generated source instead of a hand-written
# approximation.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _quiet_codegen_logging():
    """Silence the generators' INFO/WARNING logging while validating."""
    previous = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _raccoon_importable() -> bool:
    try:
        return importlib.util.find_spec("raccoon") is not None
    except (ImportError, ValueError):
        return False


def _check_generation(result: ValidationResult, config: dict) -> Optional[Set[str]]:
    """Run the generators in memory; ERROR for anything codegen would reject.

    Returns the attribute names of the generated ``Defs`` class, or None when
    they could not be produced.
    """
    if not _raccoon_importable():
        result.add(ValidationIssue(
            severity=Severity.ERROR,
            code="codegen_check_unavailable",
            message="Cannot check the project: the 'raccoon' package is not importable",
            hint=(
                "Install raccoon-library (it provides the hardware types codegen "
                "needs), or pass --no-validate to skip this check."
            ),
        ))
        return None

    from raccoon_cli.codegen.generators.defs_generator import DefsGenerator
    from raccoon_cli.codegen.generators.defs_stub_generator import DefsStubGenerator
    from raccoon_cli.codegen.generators.robot_generator import RobotGenerator

    # Fresh instances every time: generators keep state between calls.
    factories = (
        ("defs.py", lambda: DefsGenerator(class_name="Defs")),
        ("defs.pyi", lambda: DefsStubGenerator(class_name="Defs")),
        ("robot.py", lambda: RobotGenerator(class_name="Robot")),
    )

    sources: dict[str, str] = {}
    reported: set[str] = set()
    with _quiet_codegen_logging():
        for label, factory in factories:
            try:
                sources[label] = factory().generate(config)
            except Exception as exc:  # noqa: BLE001 - any failure here fails codegen too
                message = str(exc)
                if message in reported:
                    continue  # defs.py and defs.pyi usually fail identically
                reported.add(message)
                result.add(ValidationIssue(
                    severity=Severity.ERROR,
                    code="codegen_failed",
                    message=f"Code generation for {label} would fail: {message}",
                    hint="Fix raccoon.project.yml, then run 'raccoon codegen'.",
                ))

    if "defs.py" not in sources:
        return None
    _check_generated_types(result, sources["defs.py"])
    return _defs_class_attributes_from_source(sources["defs.py"])


def _check_generated_types(result: ValidationResult, source: str) -> None:
    """Type-check the generated ``defs.py`` against raccoon-library's stubs.

    Codegen copies config values into constructor calls verbatim, so a typo such
    as ``static_friction_pct: 1ß`` (a string) only crashes on the robot at
    import time. The stubs are installed next to raccoon-cli, so pyright can
    catch it here. Without pyright a warning is reported and the check is skipped.
    """
    # Next to the interpreter first: the project venv need not be on PATH.
    pyright = shutil.which("pyright", path=str(Path(sys.executable).parent)) or shutil.which("pyright")
    if pyright is None:
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="type_check_skipped",
            message="Type check of the hardware config skipped: pyright is not installed",
            hint="Run 'uv add pyright' in the project to catch wrong value types before the run.",
        ))
        return
    with tempfile.TemporaryDirectory(prefix="raccoon-typecheck-") as tmp:
        path = Path(tmp) / "defs.py"
        path.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [pyright, "--outputjson", "--pythonpath", sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=tmp,
            )
            diagnostics = json.loads(proc.stdout)["generalDiagnostics"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            logger.debug("type check of defs.py failed to run: %s", exc)
            return
    for diag in diagnostics:
        # reportMissingModuleSource is a warning: only the stubs exist off the robot.
        if diag.get("severity") != "error":
            continue
        line = diag.get("range", {}).get("start", {}).get("line", 0) + 1
        message = str(diag.get("message", "")).splitlines()[0]
        result.add(ValidationIssue(
            severity=Severity.ERROR,
            code="defs_type_error",
            message=f"Generated defs.py line {line}: {message}",
            hint="Fix the value in the hardware config (e.g. config/motors.yml), then run 'raccoon codegen'.",
        ))


def _defs_bindings(tree: ast.AST) -> tuple[Set[str], Set[str]]:
    """Return the local names a file binds to the Defs class/singleton and to
    the ``defs`` module."""
    class_names: Set[str] = set()
    module_names: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            last = (node.module or "").split(".")[-1]
            for alias in node.names:
                local = alias.asname or alias.name
                if last == "defs" and alias.name in _DEFS_ACCESS_NAMES:
                    class_names.add(local)  # from x.defs import Defs [as D]
                elif last != "defs" and alias.name == "defs":
                    module_names.add(local)  # from x import defs [as m]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[-1] == "defs":
                    module_names.add(alias.asname)  # import x.defs as m

    return class_names, module_names


def _is_robot_expr(node: ast.AST) -> bool:
    """``robot`` or ``self.robot`` — the only bases treated as a Robot."""
    if isinstance(node, ast.Name):
        return node.id == "robot"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "robot"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _check_defs_attribute_access(
    result: ValidationResult,
    project_root: Path,
    valid_names: Set[str],
) -> None:
    """ERROR for any ``Defs.<attr>`` access where ``<attr>`` is not a real
    hardware object — this would raise ``AttributeError`` at runtime.

    Recognised access forms::

        Defs.x   /   D.x   (from ... import Defs [as D])
        m.Defs.x /  m.defs.x   (from ... import defs [as m], import ... as m)
        robot.defs.x  /  self.robot.defs.x
    """
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

        class_names, module_names = _defs_bindings(tree)
        if class_names:
            # Importing either Defs or defs from a *.defs module means bare
            # ``Defs.x`` / ``defs.x`` in this file refer to it.
            class_names = class_names | _DEFS_ACCESS_NAMES

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue

            owner = node.value
            if isinstance(owner, ast.Name) and owner.id in class_names:
                pass  # Defs.x
            elif (
                isinstance(owner, ast.Attribute)
                and owner.attr in _DEFS_ACCESS_NAMES
                and isinstance(owner.value, ast.Name)
                and owner.value.id in module_names
            ):
                pass  # m.Defs.x
            elif (
                isinstance(owner, ast.Attribute)
                and owner.attr == "defs"
                and _is_robot_expr(owner.value)
            ):
                pass  # robot.defs.x
            else:
                continue

            attr = node.attr
            if attr.startswith("_"):
                continue  # dunder / private access — not a hardware object
            if attr in valid_names:
                continue

            base = ast.unparse(owner)
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
# Mission length check
#
# A mission's ``sequence()`` returns ``seq([...])``.  The number of steps in that
# root list is a rough measure of how much a single mission does.  Long missions
# are hard to read and to debug, so validate warns once the root list exceeds
# ``validation.max_mission_steps`` (raccoon.project.yml).  Setting the limit unreasonably
# high would silence that warning, so a limit above
# ``MAX_MISSION_STEPS_WARN_FACTOR`` times the default always warns as well.
# ---------------------------------------------------------------------------

DEFAULT_MAX_MISSION_STEPS = 25
MAX_MISSION_STEPS_WARN_FACTOR = 3

_SEQ_CALL_NAMES = frozenset({"seq", "Sequential"})


def _call_name(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _root_steps(source: str) -> Optional[List[ast.expr]]:
    """Steps in the list literal of ``return seq([...])`` in ``sequence()``.

    Returns None when the file does not parse or ``sequence()`` does not return
    a ``seq([...])`` call with a list literal.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for fn in ast.walk(tree):
        if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == "sequence"):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and _call_name(node.value.func) in _SEQ_CALL_NAMES
                and node.value.args
                and isinstance(node.value.args[0], ast.List)
            ):
                return list(node.value.args[0].elts)
    return None


def _validation_options(result: ValidationResult, config: dict) -> dict:
    """The ``validation:`` section of the project config (empty when absent)."""
    section = config.get("validation")
    if section is None:
        return {}
    if not isinstance(section, dict):
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="invalid_validation_section",
            message=f"validation must be a mapping of options, got {section!r}",
            hint="Using the default validation options",
        ))
        return {}
    return section


def _resolve_max_mission_steps(result: ValidationResult, options: dict) -> int:
    """Read ``validation.max_mission_steps``, warning about unusable values."""
    raw = options.get("max_mission_steps", DEFAULT_MAX_MISSION_STEPS)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="invalid_max_mission_steps",
            message=f"validation.max_mission_steps must be a positive integer, got {raw!r}",
            hint=f"Using the default of {DEFAULT_MAX_MISSION_STEPS}",
        ))
        return DEFAULT_MAX_MISSION_STEPS

    ceiling = DEFAULT_MAX_MISSION_STEPS * MAX_MISSION_STEPS_WARN_FACTOR
    if raw > ceiling:
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="max_mission_steps_too_high",
            message=(
                f"validation.max_mission_steps is set to {raw}, more than "
                f"{MAX_MISSION_STEPS_WARN_FACTOR}x the default ({DEFAULT_MAX_MISSION_STEPS})"
            ),
            hint="Missions this long are hard to read and debug. Split them into smaller missions or sub-sequences",
        ))
    return raw


def _check_mission_length(
    result: ValidationResult,
    config: dict,
    project_root: Path,
) -> None:
    """WARNING for every mission whose root ``seq([...])`` exceeds ``validation.max_mission_steps``."""
    options = _validation_options(result, config)
    limit = _resolve_max_mission_steps(result, options)
    check_comments = _resolve_check_mission_comments(result, options)

    missions_dir = project_root / "src" / "missions"
    if not missions_dir.is_dir():
        return

    for py_file in sorted(missions_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        steps = _root_steps(source)
        if steps is None:
            continue
        rel = py_file.relative_to(project_root)
        if len(steps) > limit:
            result.add(ValidationIssue(
                severity=Severity.WARNING,
                code="mission_too_long",
                message=(
                    f"{rel} has {len(steps)} steps in its "
                    f"sequence (validation.max_mission_steps: {limit})"
                ),
                hint="Split the mission into several missions or group steps into sub-sequences",
            ))
        _check_repeated_steps(result, rel, steps)
        if check_comments:
            _check_uncommented_steps(result, rel, steps, source)


def _resolve_check_mission_comments(result: ValidationResult, options: dict) -> bool:
    """Read ``validation.check_mission_comments`` (default: on)."""
    raw = options.get("check_mission_comments", True)
    if not isinstance(raw, bool):
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="invalid_check_mission_comments",
            message=f"validation.check_mission_comments must be true or false, got {raw!r}",
            hint="Using the default (true)",
        ))
        return True
    return raw


# A step (or block of steps) written out this many times in a row should be a loop.
LOOP_HINT_MIN_REPEATS = 3


def _repeated_runs(steps: List[ast.expr]) -> List[tuple[int, int, int]]:
    """Back-to-back repetitions in ``steps`` as ``(start, block_len, repeats)``.

    A block of ``block_len`` steps that appears ``repeats >= LOOP_HINT_MIN_REPEATS``
    times directly after each other. The shortest repeating block wins.
    """
    dumps = [ast.dump(step) for step in steps]
    runs: List[tuple[int, int, int]] = []
    i = 0
    while i < len(dumps):
        for block_len in range(1, (len(dumps) - i) // LOOP_HINT_MIN_REPEATS + 1):
            block = dumps[i:i + block_len]
            repeats = 1
            while dumps[i + repeats * block_len:i + (repeats + 1) * block_len] == block:
                repeats += 1
            if repeats >= LOOP_HINT_MIN_REPEATS:
                runs.append((i, block_len, repeats))
                i += block_len * repeats - 1
                break
        i += 1
    return runs


# More than this many steps in a row without a comment trigger a warning.
MAX_UNCOMMENTED_STEPS = 6


def _comment_lines(source: str) -> tuple[Set[int], Set[int]]:
    """Line numbers holding a comment, and those holding only a comment."""
    all_lines: Set[int] = set()
    only_lines: Set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                all_lines.add(tok.start[0])
                if not tok.line[:tok.start[1]].strip():
                    only_lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return all_lines, only_lines


def _check_uncommented_steps(
    result: ValidationResult,
    rel_path: Path,
    steps: List[ast.expr],
    source: str,
) -> None:
    """WARNING for each stretch of more than MAX_UNCOMMENTED_STEPS steps without a comment.

    A step counts as commented when a comment sits on the line directly above it
    or on its first line. A comment covers only that one step.
    """
    any_comment, comment_only = _comment_lines(source)
    run: List[ast.expr] = []

    def flush() -> None:
        if len(run) > MAX_UNCOMMENTED_STEPS:
            result.add(ValidationIssue(
                severity=Severity.WARNING,
                code="mission_uncommented",
                message=(
                    f"{rel_path}:{run[0].lineno}-{run[-1].end_lineno} has {len(run)} steps "
                    f"in a row without a comment"
                ),
                hint="Add a short comment above the steps that explains what this part of the mission does",
            ))

    for step in steps:
        if (step.lineno - 1) in comment_only or step.lineno in any_comment:
            flush()
            run = []
        else:
            run.append(step)
    flush()


def _check_repeated_steps(
    result: ValidationResult,
    rel_path: Path,
    steps: List[ast.expr],
) -> None:
    """WARNING when the same step(s) are written out repeatedly in a row; use loop_for."""
    for start, block_len, repeats in _repeated_runs(steps):
        first = steps[start].lineno
        what = "step" if block_len == 1 else f"block of {block_len} steps"
        example = "loop_for(<step>, iterations=N)" if block_len == 1 else "loop_for(seq([...]), iterations=N)"
        result.add(ValidationIssue(
            severity=Severity.WARNING,
            code="repeated_steps",
            message=(
                f"{rel_path}:{first} repeats the same {what} {repeats} times in a row"
            ),
            hint=f"Use a loop instead: {example}".replace("N", str(repeats)),
        ))


# ---------------------------------------------------------------------------
# Single-element composite check
#
# ``seq([step])`` runs exactly like ``step``, and ``parallel(branch)`` has
# nothing to run its only branch in parallel with. The step that a mission's
# ``sequence()`` returns is accepted either way; the steps inside it are checked.
# ---------------------------------------------------------------------------

def _single_composite_name(node: ast.AST) -> Optional[str]:
    """``"seq"`` or ``"parallel"`` when *node* is such a call with one element.

    Unpacked arguments (``*steps``) have an unknown length and never count.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if len(node.args) != 1 or node.keywords or isinstance(node.args[0], ast.Starred):
        return None
    if node.func.id == "parallel":
        return "parallel"
    if node.func.id == "seq":
        steps = node.args[0]
        if (
            isinstance(steps, ast.List)
            and len(steps.elts) == 1
            and not isinstance(steps.elts[0], ast.Starred)
        ):
            return "seq"
    return None


def _returned_mission_steps(tree: ast.AST) -> Set[ast.expr]:
    """The expressions that a ``sequence()`` method returns."""
    return {
        node.value
        for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "sequence"
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and node.value is not None
    }


def _check_single_element_composites(result: ValidationResult, project_root: Path) -> None:
    """WARNING for every ``seq([...])`` or ``parallel(...)`` holding one element."""
    src_root = project_root / "src"
    if not src_root.exists():
        return

    for py_file in sorted(src_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, ValueError):
            continue  # syntax errors are surfaced by the compile check

        rel = py_file.relative_to(project_root)
        is_mission = rel.parts[:2] == ("src", "missions")
        accepted = _returned_mission_steps(tree) if is_mission else set()
        found = [
            (node.lineno, node.col_offset, name)
            for node in ast.walk(tree)
            if node not in accepted and (name := _single_composite_name(node)) is not None
        ]
        for lineno, _, name in sorted(found):
            if name == "seq":
                message = f"{rel}:{lineno} uses seq([...]) with only one step"
                hint = "No seq() needed here: use the step directly"
            else:
                message = f"{rel}:{lineno} uses parallel(...) with only one argument"
                hint = (
                    "No parallel() needed here: use the step directly, "
                    "or seq([...]) for a list of steps"
                )
            result.add(ValidationIssue(
                severity=Severity.WARNING,
                code=f"single_element_{name}",
                message=message,
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
    codegen_check: bool = True,
) -> ValidationResult:
    """Run all project consistency checks and return a structured result.

    Checks performed:
    - Required config keys (name, uuid)
    - Config entries → mission files on disk
    - Mission files on disk → config entries
    - main.py imports → files + config
    - Python compile check for src/**/*.py (optional)
    - Code generation, run in memory — reports everything ``raccoon codegen``
      would reject (optional). Requires the ``raccoon`` package to be
      importable; if it is not, that is itself an error.
    - Single-element composites — warns for ``seq([...])`` with one step and
      ``parallel(...)`` with one argument anywhere under src/, except the step
      that a mission's ``sequence()`` returns
    - Uncommented steps — warns when more than 6 steps in a row have no comment
      (disable with ``validation.check_mission_comments: false``)
    - Repeated steps — warns when the same step or block of steps appears 3 or
      more times in a row in a mission's root ``seq([...])`` (use ``loop_for``)
    - Mission length — warns when a mission's root ``seq([...])`` has more
      steps than ``validation.max_mission_steps`` (default 25), and when that limit is
      set above 3x the default
    - Defs attribute access — flags ``Defs.<attr>`` references that would
      raise ``AttributeError`` at runtime, checked against the names the
      generators produce from the current config (optional)
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

    _check_mission_length(result, config, project_root)
    _check_single_element_composites(result, project_root)

    if python_compile:
        _check_python_compile(result, project_root)

    generated: Optional[Set[str]] = None
    if codegen_check:
        generated = _check_generation(result, config)

    if defs_check:
        valid_defs = _collect_valid_defs_attributes(config, project_root, generated)
        if valid_defs is not None:
            _check_defs_attribute_access(result, project_root, valid_defs)

    return result


def run_validation_or_exit(
    console,
    project_root: Path,
    *,
    python_compile: bool = True,
    defs_check: bool = True,
    codegen_check: bool = True,
    # Accepted for API compatibility but ignored: validation now always runs
    # the generators in memory (see ``codegen_check``).
    config=None,
    codegen_probe: bool = False,
) -> None:
    """Run validation and abort with SystemExit(1) if any errors are found."""
    result = validate_project(
        project_root,
        python_compile=python_compile,
        defs_check=defs_check,
        codegen_check=codegen_check,
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
