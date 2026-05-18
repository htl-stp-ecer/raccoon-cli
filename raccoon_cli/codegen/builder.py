"""Expression building utilities for code generation."""

from __future__ import annotations

import ast
import inspect
import logging
from typing import Any, Dict, Set, Tuple

from .introspection import infer_param_type, get_init_params

logger = logging.getLogger("raccoon")


# ---------------------------------------------------------------------------
# Internal AST helpers
# ---------------------------------------------------------------------------

def _literal_node(v: Any) -> ast.expr:
    """Recursively convert a Python value to an ast.expr node."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, float, str)):
        return ast.Constant(value=v)
    if isinstance(v, (list, tuple)):
        return ast.List(elts=[_literal_node(x) for x in v], ctx=ast.Load())
    if isinstance(v, dict):
        return ast.Dict(
            keys=[_literal_node(k) for k in v],
            values=[_literal_node(val) for val in v.values()],
        )
    # Fallback for other types (e.g. enum values): use repr as a constant
    return ast.Constant(value=repr(v))


def _constructor_node(
    cls: type,
    data: Dict[str, Any],
    context: str,
    imports: "ImportSet",
) -> ast.Call:
    """Build an ast.Call node for `ClassName(kw=value, ...)` recursively."""
    if not isinstance(data, dict):
        raise ValueError(
            f"{context}: expected mapping for {cls.__name__}, got {type(data).__name__}"
        )

    logger.info(f"Building {cls.__name__} for {context}")
    imports.add(cls)

    init_params = get_init_params(cls)

    _variadic = {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    required_params = {
        name
        for name, param in init_params.items()
        if param.default == inspect.Parameter.empty and param.kind not in _variadic
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

    valid_params = {n for n, p in init_params.items() if p.kind not in _variadic}
    unknown_params = provided_params - valid_params
    if unknown_params:
        logger.warning(
            f"{context}: Unknown parameter(s) for {cls.__name__}: "
            f"{', '.join(sorted(unknown_params))}. "
            f"Valid parameters: {', '.join(sorted(valid_params))}"
        )

    keywords: list[ast.keyword] = []
    for name, value in data.items():
        if isinstance(value, dict):
            nested_cls = infer_param_type(cls, name)
            if nested_cls:
                logger.info(f"Treating '{name}' as {nested_cls.__name__} constructor")
                value_node: ast.expr = _constructor_node(
                    nested_cls, value, f"{context}.{name}", imports
                )
            else:
                logger.debug(f"Using literal dict for '{name}'")
                value_node = _literal_node(value)
        else:
            value_node = _literal_node(value)
        keywords.append(ast.keyword(arg=name, value=value_node))

    return ast.Call(
        func=ast.Name(id=cls.__name__, ctx=ast.Load()),
        args=[],
        keywords=keywords,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_literal_expr(v: Any) -> str:
    """Convert a Python value to its literal string representation."""
    node = _literal_node(v)
    ast.fix_missing_locations(node)
    return ast.unparse(node)


class ImportSet:
    """Manages a set of imports for generated code."""

    def __init__(self) -> None:
        self._entries: Set[Tuple[str, str]] = set()

    def add(self, cls: type) -> None:
        """Add a class (or ClassProxy) to the import set."""
        if cls.__module__ == "builtins":
            return
        self._entries.add((cls.__module__, cls.__name__))

    def add_qualname(self, qualname: str) -> str:
        """Add an import by dotted qualname and return the simple class name."""
        module, name = qualname.rsplit(".", 1)
        self._entries.add((module, name))
        return name

    def render(self) -> str:
        """Render the imports as Python import statements."""
        by_mod: Dict[str, Set[str]] = {}
        for mod, name in self._entries:
            by_mod.setdefault(mod, set()).add(name)

        lines = []
        for mod in sorted(by_mod.keys()):
            names = ", ".join(sorted(by_mod[mod]))
            lines.append(f"from {mod} import {names}")
        return "\n".join(lines)


def infer_nested_class(
    parent_cls: type, param_name: str, value: Dict[str, Any]
) -> type | None:
    """Try to infer nested class from parent class's __init__ parameter type."""
    return infer_param_type(parent_cls, param_name)


def build_constructor_expr(
    cls: type,
    data: Dict[str, Any],
    context: str,
    imports: ImportSet,
) -> str:
    """
    Turn dict into 'ClassName(kw=...)' - recursively handles nested classes.

    Validates that all required parameters are provided and checks their types
    against the class signature.
    """
    node = _constructor_node(cls, data, context, imports)
    ast.fix_missing_locations(node)
    return ast.unparse(node)
