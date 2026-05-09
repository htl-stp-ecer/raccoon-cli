"""Introspection utilities for analyzing Python classes."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import logging
import typing
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("raccoon")

# Process-lifetime cache of synthesised stub classes keyed by fully-qualified name.
# Ensures identical objects are returned for the same qualname (required for issubclass).
_stub_class_registry: dict[str, type] = {}


def resolve_class(qualname: str) -> type:
    """Resolve a fully-qualified class name to a class object.

    Tries a real import first; falls back to synthesising a class from the
    installed .pyi stub when only raccoon-stubs (no runtime) is present.
    """
    if "." not in qualname:
        raise ImportError(f"Cannot resolve simple name '{qualname}' without namespace")

    module_name, class_name = qualname.rsplit(".", 1)
    try:
        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)
        return cls
    except (ImportError, AttributeError):
        pass
    return _resolve_class_from_stub(qualname)


def qualname_of(cls: type) -> str:
    """Get the fully qualified name of a class."""
    return f"{cls.__module__}.{cls.__name__}"


def _find_pyi_for_module(module_name: str) -> Optional[Path]:
    """Find the installed .pyi stub file for a module.

    Handles three cases:
    - Regular .py/.so module: sibling .pyi next to the origin file.
    - Namespace package (origin=None, has search locations): __init__.pyi in the
      first matching search location (covers raccoon, raccoon.hal, etc.).
    - .pyi-only leaf module (no .py file): located via parent's search locations.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except (ModuleNotFoundError, ValueError):
        spec = None

    if spec is not None and spec.origin is not None:
        origin = Path(spec.origin)
        stem = origin.stem.split(".")[0]
        pyi = origin.parent / f"{stem}.pyi"
        return pyi if pyi.exists() else None

    if spec is not None and spec.submodule_search_locations:
        for search_path in spec.submodule_search_locations:
            pyi = Path(search_path) / "__init__.pyi"
            if pyi.exists():
                return pyi
        return None

    # .pyi-only leaf module or module not found: look via parent's search locations.
    parts = module_name.rsplit(".", 1)
    if len(parts) < 2:
        return None
    parent_name, stem = parts
    try:
        parent_spec = importlib.util.find_spec(parent_name)
    except (ModuleNotFoundError, ValueError):
        return None
    if parent_spec is None or not parent_spec.submodule_search_locations:
        return None
    for search_path in parent_spec.submodule_search_locations:
        pyi = Path(search_path) / f"{stem}.pyi"
        if pyi.exists():
            return pyi
    return None


def _resolve_pyi_bases(class_node: ast.ClassDef, module_name: str, tree: ast.Module) -> List[type]:
    """Resolve base classes of a synthesised stub class from the AST import map."""
    import_map: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.names:
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                if node.level == 0 and node.module:
                    import_map[local_name] = f"{node.module}.{alias.name}"
                elif node.level > 0:
                    parts = module_name.split(".")
                    base_parts = parts[:-node.level] if node.level <= len(parts) else []
                    base = ".".join(base_parts)
                    source_module = f"{base}.{node.module}" if node.module else base
                    import_map[local_name] = f"{source_module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                import_map[local_name] = alias.name

    bases: List[type] = []
    for base_expr in class_node.bases:
        base_str = ast.unparse(base_expr)
        try:
            if "." in base_str:
                bases.append(resolve_class(base_str))
            elif base_str in import_map:
                bases.append(resolve_class(import_map[base_str]))
            elif base_str not in ("object", "ABC"):
                bases.append(resolve_class(f"{module_name}.{base_str}"))
        except ImportError:
            pass
    return bases


def _resolve_class_from_stub(qualname: str) -> type:
    """Synthesise a class object from an installed .pyi stub file.

    Checks the process-lifetime registry first so repeated calls for the same
    name return the identical object (required for issubclass correctness).
    """
    if qualname in _stub_class_registry:
        return _stub_class_registry[qualname]

    module_name, class_name = qualname.rsplit(".", 1)
    pyi = _find_pyi_for_module(module_name)
    if pyi is None:
        raise ImportError(
            f"Cannot resolve '{qualname}': no stub file found for '{module_name}'"
        )

    try:
        tree = ast.parse(pyi.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        raise ImportError(f"Cannot resolve '{qualname}': failed to parse {pyi}: {e}")

    # Look for a ClassDef with the target name.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            raw_bases = _resolve_pyi_bases(node, module_name, tree)
            bases = tuple(raw_bases) if raw_bases else (object,)
            cls = type(class_name, bases, {"__module__": module_name})
            _stub_class_registry[qualname] = cls
            return cls

    # Look for a re-export: `from X import Y [as Z]`
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ImportFrom) or not node.names:
            continue
        for alias in node.names:
            imported_name = alias.asname if alias.asname else alias.name
            if imported_name != class_name:
                continue
            if node.level == 0 and node.module:
                source_qualname = f"{node.module}.{alias.name}"
            elif node.level > 0:
                parts = module_name.split(".")
                base_parts = parts[:-node.level] if node.level <= len(parts) else []
                base = ".".join(base_parts)
                source_module = f"{base}.{node.module}" if node.module else base
                source_qualname = f"{source_module}.{alias.name}"
            else:
                continue
            cls = _resolve_class_from_stub(source_qualname)
            _stub_class_registry[qualname] = cls
            return cls

    raise ImportError(
        f"Cannot resolve '{qualname}': class '{class_name}' not found in {pyi}"
    )


def _parse_pyi_tree(cls: type) -> Optional[ast.ClassDef]:
    """Parse the .pyi stub for cls and return its ClassDef node, if found."""
    pyi = _find_pyi_for_module(cls.__module__)
    if pyi is None:
        return None

    try:
        tree = ast.parse(pyi.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        logger.debug(f"Failed to parse {pyi}: {e}")
        return None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            return node

    return None


def _parse_init_from_pyi(cls: type) -> Optional[Dict[str, inspect.Parameter]]:
    """Parse __init__ parameters from the class's installed .pyi stub file."""
    class_node = _parse_pyi_tree(cls)
    if class_node is None:
        return None

    # Pick the __init__ with the most parameters (handles @overload)
    best_args: list[ast.arg] = []
    best_defaults: list = []
    for item in class_node.body:
        if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
            continue
        if len(item.args.args) >= len(best_args):
            best_args = item.args.args
            best_defaults = item.args.defaults

    if not best_args:
        return None

    num_args = len(best_args)
    num_defaults = len(best_defaults)
    first_default_idx = num_args - num_defaults

    params: Dict[str, inspect.Parameter] = {}
    for i, arg in enumerate(best_args):
        if arg.arg == "self":
            continue
        has_default = i >= first_default_idx
        params[arg.arg] = inspect.Parameter(
            arg.arg,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None if has_default else inspect.Parameter.empty,
        )
    return params


def _parse_param_type_from_pyi(cls: type, param_name: str) -> Optional[type]:
    """Parse the type annotation of an __init__ parameter from the .pyi stub."""
    class_node = _parse_pyi_tree(cls)
    if class_node is None:
        return None

    for item in class_node.body:
        if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
            continue

        for arg in item.args.args:
            if arg.arg != param_name or arg.annotation is None:
                continue

            type_str = ast.unparse(arg.annotation)
            # Try as fully-qualified name first
            try:
                return resolve_class(type_str)
            except ImportError:
                pass
            # Try just the class name (strips module prefix)
            short = type_str.split(".")[-1]
            if short != type_str:
                try:
                    return resolve_class(f"raccoon.{short}")
                except ImportError:
                    pass

    return None


def get_init_params(cls: type) -> Dict[str, Any]:
    """Get __init__ parameters for a class as inspect.Parameter objects.

    Falls back to parsing the installed .pyi stub when inspect.signature fails
    (common for pybind11 native classes).
    """
    try:
        sig = inspect.signature(cls.__init__)
        params = {name: p for name, p in sig.parameters.items() if name != "self"}
        if params:
            return params
    except (ValueError, TypeError):
        pass

    pyi_params = _parse_init_from_pyi(cls)
    if pyi_params is not None:
        return pyi_params

    return {}


def infer_param_type(parent_cls: type, param_name: str) -> Optional[type]:
    """Infer the type of an __init__ parameter from type annotations.

    Falls back to parsing the .pyi stub for pybind11 classes where runtime
    type hints are unavailable.
    """
    try:
        hints = typing.get_type_hints(parent_cls.__init__)
        type_hint = hints.get(param_name)
        if type_hint is not None:
            if isinstance(type_hint, type):
                return type_hint
            if isinstance(type_hint, str):
                try:
                    return resolve_class(type_hint)
                except ImportError:
                    pass
    except Exception:
        pass

    return _parse_param_type_from_pyi(parent_cls, param_name)


# Keep old name as alias for compatibility
parse_type_from_docstring = infer_param_type
