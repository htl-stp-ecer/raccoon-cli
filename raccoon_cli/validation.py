"""Project validation helpers for raccoon CLI commands."""

from __future__ import annotations

import logging
import py_compile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from raccoon_cli.codegen import create_pipeline
from raccoon_cli.project import load_project_config

logger = logging.getLogger("raccoon")


@dataclass
class ValidationResult:
    """Validation outcome for a project."""

    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _validate_required_project_keys(config: dict) -> list[str]:
    errors: list[str] = []
    for key in ("name", "uuid"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"Missing required project key: {key}")
    return errors


def _compile_project_python(project_root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    src_root = project_root / "src"
    if not src_root.exists():
        warnings.append("No src directory found; skipping Python compile checks")
        return errors, warnings

    for py_file in sorted(src_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as exc:
            rel = py_file.relative_to(project_root)
            errors.append(f"Python compile failed for {rel}: {exc.msg}")

    return errors, warnings


def _probe_codegen_validation(config: dict) -> list[str]:
    try:
        pipeline = create_pipeline()
        with tempfile.TemporaryDirectory(prefix="raccoon-validate-") as tmp_dir:
            pipeline.run_all(config, Path(tmp_dir), format_code=False)
        return []
    except Exception as exc:
        return [f"Codegen validation failed: {exc}"]


def validate_project(
    project_root: Path,
    config: dict | None = None,
    *,
    python_compile: bool = True,
    codegen_probe: bool = True,
) -> ValidationResult:
    """Validate project config and source code before sync/run/codegen."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        cfg = config if config is not None else load_project_config(project_root)
    except Exception as exc:
        return ValidationResult(passed=False, errors=[f"Failed to load project config: {exc}"])

    if not isinstance(cfg, dict):
        return ValidationResult(passed=False, errors=["raccoon.project.yml must be a mapping"])

    errors.extend(_validate_required_project_keys(cfg))

    if codegen_probe:
        errors.extend(_probe_codegen_validation(cfg))

    if python_compile:
        compile_errors, compile_warnings = _compile_project_python(project_root)
        errors.extend(compile_errors)
        warnings.extend(compile_warnings)

    return ValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def run_validation_or_exit(
    console: Console,
    project_root: Path,
    config: dict | None = None,
    *,
    python_compile: bool = True,
    codegen_probe: bool = True,
) -> None:
    """Run validation and stop command execution if checks fail."""
    result = validate_project(
        project_root,
        config=config,
        python_compile=python_compile,
        codegen_probe=codegen_probe,
    )

    for warning in result.warnings:
        console.print(f"[yellow]Validation warning:[/yellow] {warning}")

    if not result.passed:
        console.print(f"[red]Validation failed ({len(result.errors)} issue(s))[/red]")
        for issue in result.errors:
            console.print(f"  - {issue}")
        raise SystemExit(1)

    logger.info("Validation passed")