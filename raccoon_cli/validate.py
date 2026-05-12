"""Backward-compatibility re-export — use raccoon_cli.validation instead."""
from raccoon_cli.validation import (  # noqa: F401
    Severity,
    ValidationIssue,
    ValidationResult,
    class_name_to_expected_file,
    file_name_to_expected_class,
    run_validation_or_exit,
    validate_project,
)
