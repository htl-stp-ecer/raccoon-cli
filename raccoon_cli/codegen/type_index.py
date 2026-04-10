"""Offline type index for raccoon — enables codegen without live hardware imports.

The index is a JSON file that captures class metadata (init params, bases,
docstrings) from .pyi stub files.  It is auto-generated on first access
and used as a fallback when live ``import raccoon`` is unavailable.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("raccoon")

INDEX_VERSION = 1
DEFAULT_INDEX_PATH = Path.home() / ".raccoon" / "raccoon_type_index.json"


# ---------------------------------------------------------------------------
# Find raccoon package directory
# ---------------------------------------------------------------------------

def _find_raccoon_package_dir() -> Optional[Path]:
    """Find the raccoon package directory without importing it."""
    import site

    # Check common install locations
    search_dirs = site.getsitepackages() + [site.getusersitepackages()]
    for d in search_dirs:
        candidate = Path(d) / "raccoon"
        if candidate.is_dir():
            return candidate

    return None


# ---------------------------------------------------------------------------
# .pyi stub file parsing
# ---------------------------------------------------------------------------

def _find_pyi_files(package_dir: Path) -> Dict[str, Path]:
    """Find all .pyi stub files in the raccoon package directory."""
    pyi_files = {}
    for pyi_path in package_dir.glob("*.pyi"):
        stem = pyi_path.stem
        if stem == "__init__":
            mod_name = "raccoon"
        else:
            mod_name = f"raccoon.{stem}"
        pyi_files[mod_name] = pyi_path
    return pyi_files


def _parse_init_from_ast(
    func_node: ast.FunctionDef,
) -> List[Dict[str, Any]]:
    """Extract __init__ parameters from an AST FunctionDef node."""
    params = []
    args = func_node.args

    # Count how many positional args lack defaults
    num_args = len(args.args)
    num_defaults = len(args.defaults)
    first_default_idx = num_args - num_defaults

    for i, arg in enumerate(args.args):
        if arg.arg == "self":
            continue

        type_str = ""
        if arg.annotation:
            type_str = ast.unparse(arg.annotation)

        required = i < first_default_idx
        params.append({
            "name": arg.arg,
            "type": type_str,
            "required": required,
        })

    for arg in args.kwonlyargs:
        type_str = ""
        if arg.annotation:
            type_str = ast.unparse(arg.annotation)
        params.append({
            "name": arg.arg,
            "type": type_str,
            "required": False,
        })

    return params


def _introspect_pyi_file(mod_name: str, pyi_path: Path) -> List[Dict[str, Any]]:
    """Parse a .pyi file and extract class metadata."""
    try:
        source = pyi_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError) as e:
        logger.warning(f"Failed to parse {pyi_path}: {e}")
        return []

    classes = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        qualname = f"{mod_name}.{node.name}"

        # Extract base class names
        bases = []
        for base in node.bases:
            base_str = ast.unparse(base)
            # Qualify unqualified names with the module's own namespace
            if "." not in base_str:
                base_str = f"{mod_name}.{base_str}"
            bases.append(base_str)

        # Find __init__ method (pick the non-overloaded or last one)
        init_params: List[Dict[str, Any]] = []
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                parsed = _parse_init_from_ast(item)
                # Keep the signature with the most named params (handles @overload)
                if len(parsed) >= len(init_params):
                    init_params = parsed

        classes.append({
            "qualname": qualname,
            "module": mod_name,
            "name": node.name,
            "bases": bases,
            "init_params": init_params,
        })

    return classes


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------

def generate_index(output_path: Optional[Path] = None) -> Path:
    """Generate a type index from .pyi stub files installed by raccoon-stubs.

    Returns the path to the written JSON file.
    """
    output_path = output_path or DEFAULT_INDEX_PATH

    package_dir = _find_raccoon_package_dir()
    if package_dir is None:
        raise RuntimeError(
            "No raccoon package directory found. Is raccoon-stubs installed?\n"
            "Install it with: pip install raccoon-stubs"
        )

    pyi_files = _find_pyi_files(package_dir)
    if not pyi_files:
        raise RuntimeError(
            "No .pyi stub files found in raccoon package.\n"
            "Install stubs with: pip install raccoon-stubs"
        )

    logger.info(f"Generating type index from {len(pyi_files)} .pyi stub files...")

    # Try to get version from __init__.pyi
    version = "unknown"
    init_pyi = pyi_files.get("raccoon")
    if init_pyi:
        try:
            source = init_pyi.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.iter_child_nodes(tree):
                if (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "__version__"
                    and isinstance(node.value, ast.Constant)
                ):
                    version = str(node.value.value)
                    break
        except Exception:
            pass

    classes: Dict[str, Dict[str, Any]] = {}
    module_exports: Dict[str, List[str]] = {}

    # Parse .pyi files for submodules (skip __init__.pyi for now)
    for mod_name, pyi_path in pyi_files.items():
        if mod_name == "raccoon":
            continue  # Handle __init__ separately for namespace map
        class_entries = _introspect_pyi_file(mod_name, pyi_path)
        exports = []
        for entry in class_entries:
            classes[entry["qualname"]] = entry
            exports.append(entry["name"])
        module_exports[mod_name] = exports

    # Build the top-level raccoon namespace mapping from __init__.pyi imports
    raccoon_exports: Dict[str, str] = {}
    if init_pyi:
        try:
            source = init_pyi.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        imported_name = alias.asname or alias.name
                        qualified = f"{node.module}.{alias.name}"
                        # Only include classes that exist in our index
                        if qualified in classes:
                            raccoon_exports[imported_name] = qualified
        except Exception:
            pass

    # Also map any class in submodules that isn't already mapped
    for qualname, entry in classes.items():
        name = entry["name"]
        if name not in raccoon_exports:
            raccoon_exports[name] = qualname

    module_exports["raccoon"] = sorted(raccoon_exports.keys())

    index = {
        "version": INDEX_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "raccoon_version": version,
        "classes": classes,
        "module_exports": module_exports,
        "namespace_map": raccoon_exports,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(index, indent=2, sort_keys=False), encoding="utf-8"
    )

    logger.info(
        f"Type index written to {output_path} "
        f"({len(classes)} classes, raccoon v{version})"
    )
    return output_path


# ---------------------------------------------------------------------------
# ClassProxy — lightweight stand-in for a real type during codegen
# ---------------------------------------------------------------------------

class ClassProxy:
    """A lightweight proxy that quacks like a ``type`` for codegen purposes.

    Provides ``__module__``, ``__name__``, and cached init-parameter info
    parsed from .pyi stub files at index time.
    """

    def __init__(self, entry: Dict[str, Any]):
        self.__module__ = entry["module"]
        self.__name__ = entry["name"]
        self.__qualname__ = entry.get("qualname", f"{self.__module__}.{self.__name__}")
        self._bases: List[str] = entry.get("bases", [])
        self._init_params: List[Dict[str, Any]] = entry.get("init_params", [])

    def get_cached_params(self) -> Dict[str, inspect.Parameter]:
        """Return init parameters as inspect.Parameter objects."""
        params: Dict[str, inspect.Parameter] = {}
        for p in self._init_params:
            default = inspect.Parameter.empty if p["required"] else None
            params[p["name"]] = inspect.Parameter(
                p["name"],
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=inspect.Parameter.empty,
            )
        return params

    def get_param_type(self, param_name: str) -> Optional[str]:
        """Return the type string for a parameter, or None if not found."""
        for p in self._init_params:
            if p["name"] == param_name:
                return p.get("type") or None
        return None

    def is_subclass_of(self, qualname: str) -> bool:
        """Check if this proxy's class is a subclass of the given qualname."""
        if self.__qualname__ == qualname:
            return True
        return qualname in self._bases

    def __repr__(self) -> str:
        return f"<ClassProxy {self.__qualname__}>"


# ---------------------------------------------------------------------------
# TypeIndex — loads and queries the cached index
# ---------------------------------------------------------------------------

def _get_installed_raccoon_version() -> Optional[str]:
    """Read __version__ from the installed raccoon __init__.pyi without importing."""
    package_dir = _find_raccoon_package_dir()
    if package_dir is None:
        return None
    init_pyi = package_dir / "__init__.pyi"
    if not init_pyi.exists():
        return None
    try:
        source = init_pyi.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "__version__"
                and isinstance(node.value, ast.Constant)
            ):
                return str(node.value.value)
    except Exception:
        pass
    return None


class TypeIndex:
    """Cached type index for offline codegen.

    Auto-generates the index on first access if the cache file is
    missing or stale — no separate ``raccoon index`` step required.
    """

    def __init__(self, index_path: Optional[Path] = None):
        self._path = index_path or DEFAULT_INDEX_PATH
        self._data: Optional[Dict[str, Any]] = None
        self._proxies: Dict[str, ClassProxy] = {}

    @property
    def available(self) -> bool:
        """True if the index is loaded (or was auto-generated)."""
        self._ensure_loaded()
        return self._data is not None

    def _ensure_loaded(self) -> None:
        if self._data is not None:
            return

        # Try loading existing cache
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if raw.get("version") != INDEX_VERSION:
                    logger.debug("Type index version mismatch, regenerating")
                elif raw.get("raccoon_version") != _get_installed_raccoon_version():
                    logger.info(
                        f"raccoon version changed "
                        f"({raw.get('raccoon_version')} → {_get_installed_raccoon_version()}), "
                        f"regenerating type index"
                    )
                else:
                    self._data = raw
                    logger.debug(
                        f"Loaded type index: {len(raw.get('classes', {}))} classes, "
                        f"raccoon v{raw.get('raccoon_version', '?')}"
                    )
                    return
            except (OSError, json.JSONDecodeError) as e:
                logger.debug(f"Failed to load type index: {e}, regenerating")

        # Auto-generate from .pyi stubs
        try:
            generate_index(self._path)
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = raw
            logger.info(
                f"Auto-generated type index ({len(raw.get('classes', {}))} classes)"
            )
        except Exception as e:
            logger.warning(f"Failed to auto-generate type index: {e}")
            self._data = None

    def resolve(self, qualname: str) -> Optional[ClassProxy]:
        """Resolve a fully qualified class name to a ClassProxy."""
        self._ensure_loaded()
        if self._data is None:
            return None

        if qualname in self._proxies:
            return self._proxies[qualname]

        entry = self._data.get("classes", {}).get(qualname)
        if entry is None:
            return None

        proxy = ClassProxy(entry)
        self._proxies[qualname] = proxy
        return proxy

    def resolve_by_name(self, simple_name: str) -> Optional[ClassProxy]:
        """Resolve a simple class name via the namespace map (raccoon re-exports)."""
        self._ensure_loaded()
        if self._data is None:
            return None
        ns_map = self._data.get("namespace_map", {})
        qualname = ns_map.get(simple_name)
        if qualname:
            return self.resolve(qualname)
        return None

    def get_version(self) -> Optional[str]:
        """Return the raccoon version the index was built from."""
        self._ensure_loaded()
        if self._data is None:
            return None
        return self._data.get("raccoon_version")


# Module-level singleton, lazily loaded
_index: Optional[TypeIndex] = None


def get_type_index() -> TypeIndex:
    """Get the module-level TypeIndex singleton."""
    global _index
    if _index is None:
        _index = TypeIndex()
    return _index
