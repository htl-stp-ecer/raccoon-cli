"""Offline type index for libstp — enables codegen without live hardware imports.

The index is a JSON file that captures class metadata (init params, bases,
docstrings) from pybind11 .so modules.  It is generated once via
``raccoon index`` and then used as a fallback when live ``import libstp``
would segfault or is unavailable.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("raccoon")

INDEX_VERSION = 1
DEFAULT_INDEX_PATH = Path.home() / ".raccoon" / "libstp_type_index.json"

# pybind11 .so modules that contain types used by codegen.
# Order matters: foundation must load before hal (hal depends on it).
_SO_MODULES: List[str] = [
    "libstp._core",
    "libstp.foundation",
    "libstp.hal",
    "libstp.kinematics",
    "libstp.odometry",
    "libstp.drive",
    "libstp.motion",
    "libstp.kinematics_differential",
    "libstp.kinematics_mecanum",
    "libstp.odometry_fused",
    "libstp.sensor_ir",
    "libstp.sensor_et",
]


# ---------------------------------------------------------------------------
# Index generation (introspects live .so modules)
# ---------------------------------------------------------------------------

def _find_libstp_package_dir() -> Optional[Path]:
    """Find the libstp package directory without importing it."""
    import sysconfig
    import site

    # Check common install locations
    search_dirs = site.getsitepackages() + [site.getusersitepackages()]
    for d in search_dirs:
        candidate = Path(d) / "libstp"
        if candidate.is_dir():
            return candidate

    return None


def _find_so_paths(package_dir: Path) -> Dict[str, str]:
    """Find all .so files for the modules we need."""
    import sysconfig

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    paths = {}

    for mod_name in _SO_MODULES:
        submod = mod_name.split(".")[-1]
        candidate = package_dir / f"{submod}{ext_suffix}"
        if candidate.exists():
            paths[mod_name] = str(candidate)

    return paths


def _load_so_modules_isolated() -> Dict[str, Any]:
    """Load pybind11 .so modules without triggering libstp.__init__."""
    # Step 1: Find .so paths BEFORE touching sys.modules
    package_dir = _find_libstp_package_dir()
    if package_dir is None:
        return {}

    so_paths = _find_so_paths(package_dir)
    if not so_paths:
        return {}

    # Step 2: Install a dummy libstp package to prevent __init__.py from running
    saved_libstp = sys.modules.get("libstp")
    dummy = type(sys)("libstp")
    dummy.__path__ = [str(package_dir)]
    sys.modules["libstp"] = dummy

    # Step 3: Load each .so module in order
    loaded = {}
    for mod_name in _SO_MODULES:
        so_path = so_paths.get(mod_name)
        if not so_path:
            logger.debug(f"No .so found for {mod_name}, skipping")
            continue

        try:
            spec = importlib.util.spec_from_file_location(mod_name, so_path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            loaded[mod_name] = mod
            logger.debug(f"Loaded {mod_name} from {so_path}")
        except Exception as e:
            logger.warning(f"Failed to load {mod_name}: {e}")

    return loaded


def _parse_init_docstring(doc: str) -> List[Dict[str, Any]]:
    """Parse pybind11 __init__ docstring into parameter list.

    Handles overloaded signatures — picks the one with the most named params.
    """
    if not doc:
        return []

    # Find all __init__ signatures
    candidates = []
    for match in re.finditer(
        r"__init__\(self[^,]*(?:,\s*(.+?))?\)\s*->\s*None", doc
    ):
        params_str = match.group(1)
        if not params_str:
            candidates.append([])
            continue

        params = _parse_params_str(params_str)
        candidates.append(params)

    if not candidates:
        return []

    # Return the signature with most named (non-positional) parameters
    return max(candidates, key=lambda ps: sum(1 for p in ps if p["name"] != "arg0"))


def _parse_params_str(params_str: str) -> List[Dict[str, Any]]:
    """Split a pybind11 parameter string into structured param dicts."""
    parts = []
    depth = 0
    current: List[str] = []
    for char in params_str + ",":
        if char in "([{<":
            depth += 1
            current.append(char)
        elif char in ")]}>" :
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)

    params = []
    for part in parts:
        if not part or part.lstrip().startswith("*"):
            continue

        if "=" in part:
            name_type, _default = part.split("=", 1)
            name = name_type.split(":")[0].strip()
            type_str = name_type.split(":", 1)[1].strip() if ":" in name_type else ""
            params.append({
                "name": name,
                "type": type_str,
                "required": False,
            })
        else:
            name = part.split(":")[0].strip()
            type_str = part.split(":", 1)[1].strip() if ":" in part else ""
            params.append({
                "name": name,
                "type": type_str,
                "required": True,
            })

    return params


def _introspect_class(cls: type) -> Dict[str, Any]:
    """Extract metadata from a single class."""
    qualname = f"{cls.__module__}.{cls.__name__}"

    # Get base classes (skip pybind11_object and object)
    bases = []
    for b in cls.__mro__[1:]:
        if b is object or "pybind11" in b.__module__:
            continue
        bases.append(f"{b.__module__}.{b.__name__}")

    # Parse init params from docstring (pybind11 classes)
    doc = getattr(getattr(cls, "__init__", None), "__doc__", None) or ""
    params = _parse_init_docstring(doc)

    # Extract the raw init docstring lines for parse_type_from_docstring compat
    init_doc_lines = []
    for line in doc.strip().splitlines():
        line = line.strip()
        if "__init__" in line:
            init_doc_lines.append(line)

    return {
        "qualname": qualname,
        "module": cls.__module__,
        "name": cls.__name__,
        "bases": bases,
        "init_params": params,
        "init_docstring": "\n".join(init_doc_lines) if init_doc_lines else "",
    }


def generate_index(output_path: Optional[Path] = None) -> Path:
    """Generate a type index from the locally installed libstp .so modules.

    Returns the path to the written JSON file.
    """
    output_path = output_path or DEFAULT_INDEX_PATH

    logger.info("Loading libstp .so modules for indexing...")
    modules = _load_so_modules_isolated()

    if not modules:
        raise RuntimeError(
            "No libstp .so modules found. Is libstp installed?\n"
            "Install it with: pip install libstp"
        )

    # Get libstp version
    version = "unknown"
    core = modules.get("libstp._core")
    if core and hasattr(core, "__version__"):
        version = core.__version__

    classes: Dict[str, Dict[str, Any]] = {}
    module_exports: Dict[str, List[str]] = {}

    for mod_name, mod in modules.items():
        exports = []
        for attr_name in sorted(dir(mod)):
            obj = getattr(mod, attr_name)
            if not isinstance(obj, type):
                continue
            if "pybind11" in obj.__name__:
                continue

            info = _introspect_class(obj)
            classes[info["qualname"]] = info
            exports.append(attr_name)

        module_exports[mod_name] = exports

    # Build the top-level libstp namespace mapping (mimics __init__.py re-exports)
    libstp_exports = {}
    for mod_name, mod in modules.items():
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if isinstance(obj, type) and "pybind11" not in obj.__name__:
                libstp_exports[attr_name] = f"{obj.__module__}.{obj.__name__}"
    module_exports["libstp"] = sorted(libstp_exports.keys())

    index = {
        "version": INDEX_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "libstp_version": version,
        "classes": classes,
        "module_exports": module_exports,
        "namespace_map": libstp_exports,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(index, indent=2, sort_keys=False), encoding="utf-8"
    )

    logger.info(
        f"Type index written to {output_path} "
        f"({len(classes)} classes, libstp v{version})"
    )
    return output_path


# ---------------------------------------------------------------------------
# ClassProxy — lightweight stand-in for a real type during codegen
# ---------------------------------------------------------------------------

class _FakeInit:
    """Mimics __init__ with a __doc__ attribute for pybind11 docstring parsing."""

    def __init__(self, doc: str):
        self.__doc__ = doc


class ClassProxy:
    """A lightweight proxy that quacks like a ``type`` for codegen purposes.

    Provides ``__module__``, ``__name__``, ``__init__.__doc__`` (for pybind11
    signature parsing), and cached init-parameter info.
    """

    def __init__(self, entry: Dict[str, Any]):
        self.__module__ = entry["module"]
        self.__name__ = entry["name"]
        self.__qualname__ = entry.get("qualname", f"{self.__module__}.{self.__name__}")
        self.__init__ = _FakeInit(entry.get("init_docstring", ""))
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
                if raw.get("version") == INDEX_VERSION:
                    self._data = raw
                    logger.debug(
                        f"Loaded type index: {len(raw.get('classes', {}))} classes, "
                        f"libstp v{raw.get('libstp_version', '?')}"
                    )
                    return
                else:
                    logger.debug("Type index version mismatch, regenerating")
            except (OSError, json.JSONDecodeError) as e:
                logger.debug(f"Failed to load type index: {e}, regenerating")

        # Auto-generate from locally installed libstp
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
        """Resolve a simple class name via the namespace map (libstp re-exports)."""
        self._ensure_loaded()
        if self._data is None:
            return None
        ns_map = self._data.get("namespace_map", {})
        qualname = ns_map.get(simple_name)
        if qualname:
            return self.resolve(qualname)
        return None

    def get_version(self) -> Optional[str]:
        """Return the libstp version the index was built from."""
        self._ensure_loaded()
        if self._data is None:
            return None
        return self._data.get("libstp_version")


# Module-level singleton, lazily loaded
_index: Optional[TypeIndex] = None


def get_type_index() -> TypeIndex:
    """Get the module-level TypeIndex singleton."""
    global _index
    if _index is None:
        _index = TypeIndex()
    return _index
