"""Introspection utilities for analyzing Python classes."""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Dict, Union, get_origin, get_args

logger = logging.getLogger("raccoon")


def resolve_class(qualname: str) -> type:
    """Resolve a fully qualified class name to the actual class object."""
    mod_name, cls_name = qualname.rsplit(".", 1)
    module = __import__(mod_name, fromlist=[cls_name])
    return getattr(module, cls_name)


def qualname_of(cls: type) -> str:
    """Get the fully qualified name of a class."""
    return f"{cls.__module__}.{cls.__name__}"


def is_variadic(p: inspect.Parameter) -> bool:
    """Check if a parameter is variadic (*args or **kwargs)."""
    return p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)


def parse_pybind11_signature(cls: type) -> Dict[str, inspect.Parameter]:
    """Parse pybind11 __init__ signature from docstring."""
    doc = cls.__init__.__doc__
    if not doc:
        return {}

    # Example: __init__(self: libstp.hal.Motor, port: int, inverted: bool = False, calibration: MotorCalibration = ...) -> None
    # Note: Can't use [^)]+ because default values may contain nested parens like MotorCalibration(ff=Feedforward(...))
    match = re.search(r'__init__\(self[^,]*, (.+)\) -> None', doc)
    if not match:
        return {}

    params_str = match.group(1)
    params: Dict[str, inspect.Parameter] = {}

    # Split by comma, but handle nested types
    parts = []
    depth = 0
    current = []
    for char in params_str + ',':
        if char in '([{':
            depth += 1
            current.append(char)
        elif char in ')]}':
            depth -= 1
            current.append(char)
        elif char == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(char)

    for part in parts:
        if not part or part.lstrip().startswith('*'):
            continue
        # Parse "name: type = default" or "name: type"
        if '=' in part:
            name_type, default_str = part.split('=', 1)
            name = name_type.split(':')[0].strip()
            default = inspect.Parameter.empty if default_str.strip() in ('...', '<') else None
            params[name] = inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default, annotation=inspect.Parameter.empty
            )
        else:
            name = part.split(':')[0].strip()
            params[name] = inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=inspect.Parameter.empty, annotation=inspect.Parameter.empty
            )

    return params


def get_init_params(cls: type) -> Dict[str, inspect.Parameter]:
    """Get __init__ parameters for a class, handling both regular and pybind11 classes."""
    try:
        sig = inspect.signature(cls.__init__)
        params: Dict[str, inspect.Parameter] = {}
        for name, p in sig.parameters.items():
            if name == "self" or is_variadic(p):
                continue
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                params[name] = p
        return params
    except (ValueError, TypeError):
        # Fallback for pybind11 classes - parse from __doc__
        return parse_pybind11_signature(cls)


def unwrap_optional(tp: Any) -> Any:
    """Optional[T] -> T; Union[T, None] -> T; otherwise unchanged."""
    origin = get_origin(tp)
    if origin is Union:
        args = tuple(a for a in get_args(tp) if a is not type(None))  # noqa: E721
        if len(args) == 1:
            return args[0]
    return tp


def is_class_annotation(tp: Any) -> bool:
    """True if annotation looks like a concrete class type (not typing, not builtins container)."""
    if tp is inspect._empty:
        return False
    tp = unwrap_optional(tp)
    if isinstance(tp, type):
        # Exclude builtins
        return tp.__module__ not in ("builtins", "typing")
    # If it's a typing construct like list[T], dict[…], etc., we don't treat it as a nested class.
    return False


def parse_type_from_docstring(parent_cls: type, param_name: str) -> type | None:
    """Parse parameter type from pybind11 docstring."""
    doc = parent_cls.__init__.__doc__
    if not doc:
        logger.warning(f"No docstring for {parent_cls.__name__}.__init__")
        return None

    # Look for parameter with type annotation in docstring
    # Example: "calibration: libstp.foundation.MotorCalibration"
    pattern = rf'\b{re.escape(param_name)}\s*:\s*([a-zA-Z_.]+)'
    match = re.search(pattern, doc)
    if not match:
        logger.warning(f"No type found for parameter '{param_name}' in {parent_cls.__name__}")
        return None

    type_str = match.group(1)
    logger.debug(f"Found type for '{param_name}': {type_str}")

    # Try to resolve the type
    try:
        resolved = resolve_class(type_str)
        logger.debug(f"Resolved {type_str} to {resolved}")
        return resolved
    except (ImportError, AttributeError):
        # Try without module prefix (just class name)
        class_name = type_str.split('.')[-1]
        logger.debug(f"Failed to resolve {type_str}, trying {class_name}...")
        for module_name in ['libstp.foundation', 'libstp.hal']:
            try:
                resolved = resolve_class(f"{module_name}.{class_name}")
                logger.debug(f"Resolved to {resolved}")
                return resolved
            except (ImportError, AttributeError):
                continue

    logger.warning(f"Could not resolve type {type_str}")
    return None
