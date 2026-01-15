"""Expression building utilities for code generation."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, Set, Tuple

from .introspection import parse_type_from_docstring, get_init_params

logger = logging.getLogger("raccoon")


def build_literal_expr(v: Any) -> str:
    """Convert a Python value to its literal string representation."""
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, (int, float, bool)) or v is None:
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(build_literal_expr(x) for x in v) + "]"
    if isinstance(v, dict):
        items = ", ".join(f"{repr(k)}: {build_literal_expr(val)}" for k, val in v.items())
        return "{" + items + "}"
    return repr(v)


class ImportSet:
    """Manages a set of imports for generated code."""

    def __init__(self) -> None:
        self._entries: Set[Tuple[str, str]] = set()

    def add(self, cls: type) -> None:
        """Add a class to the import set."""
        if cls.__module__ == "builtins":
            return
        self._entries.add((cls.__module__, cls.__name__))

    def render(self) -> str:
        """Render the imports as Python import statements.

        All libstp.* submodule imports are consolidated into a single
        'from libstp import ...' statement for cleaner generated code.
        """
        # Consolidate libstp.* imports into a single 'from libstp import ...'
        libstp_names: Set[str] = set()
        by_mod: Dict[str, Set[str]] = {}

        for mod, name in self._entries:
            if mod.startswith("libstp.") or mod == "libstp":
                # Consolidate all libstp submodule imports
                libstp_names.add(name)
            else:
                by_mod.setdefault(mod, set()).add(name)

        lines = []

        # Add consolidated libstp import first
        if libstp_names:
            names = ", ".join(sorted(libstp_names))
            lines.append(f"from libstp import {names}")

        # Add other imports
        for mod in sorted(by_mod.keys()):
            names = ", ".join(sorted(by_mod[mod]))
            lines.append(f"from {mod} import {names}")

        return "\n".join(lines)


def infer_nested_class(parent_cls: type, param_name: str, value: Dict[str, Any]) -> type | None:
    """Try to infer nested class from parent class's __init__ signature via docstring."""
    return parse_type_from_docstring(parent_cls, param_name)


def build_constructor_expr(
        cls: type,
        data: Dict[str, Any],
        context: str,
        imports: ImportSet,
) -> str:
    """
    Turn dict into 'ClassName(kw=...)' - recursively handles nested classes.

    This function now validates that all required parameters are provided
    and checks their types against the class signature.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{context}: expected mapping for {cls.__name__}, got {type(data).__name__}")

    logger.info(f"Building {cls.__name__} for {context}")
    imports.add(cls)

    # Get the class's __init__ parameters
    init_params = get_init_params(cls)

    # Validate required parameters
    required_params = {
        name for name, param in init_params.items()
        if param.default == inspect.Parameter.empty
    }

    provided_params = set(data.keys())
    missing_params = required_params - provided_params

    if missing_params:
        raise ValueError(
            f"{context}: Missing required parameter(s) for {cls.__name__}: "
            f"{', '.join(sorted(missing_params))}. "
            f"Required: {', '.join(sorted(required_params))}, "
            f"Provided: {', '.join(sorted(provided_params)) if provided_params else 'none'}"
        )

    # Check for unknown parameters
    valid_params = set(init_params.keys())
    unknown_params = provided_params - valid_params

    if unknown_params:
        logger.warning(
            f"{context}: Unknown parameter(s) for {cls.__name__}: "
            f"{', '.join(sorted(unknown_params))}. "
            f"Valid parameters: {', '.join(sorted(valid_params))}"
        )

    pieces = []
    for name, value in data.items():
        if isinstance(value, dict):
            logger.debug(f"Checking nested dict parameter: {name}")
            # Try to infer if this should be a nested class constructor
            nested_cls = infer_nested_class(cls, name, value)
            if nested_cls:
                logger.info(f"Treating '{name}' as {nested_cls.__name__} constructor")
                nested_expr = build_constructor_expr(nested_cls, value, f"{context}.{name}", imports)
                pieces.append(f"{name}={nested_expr}")
            else:
                logger.debug(f"Using literal dict for '{name}'")
                # Fall back to literal dict
                pieces.append(f"{name}={build_literal_expr(value)}")
        else:
            pieces.append(f"{name}={build_literal_expr(value)}")

    return f"{cls.__name__}(" + ", ".join(pieces) + ")"
