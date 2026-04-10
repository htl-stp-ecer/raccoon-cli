"""Round-trip YAML utilities that preserve comments and formatting."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.representer import RoundTripRepresenter


# ---------------------------------------------------------------------------
# !include / !include-merge support
# ---------------------------------------------------------------------------

# Thread-local stack of base directories for resolving relative !include paths.
_tls = threading.local()


def _get_base_dir_stack() -> list[Path]:
    """Return the per-thread base-dir stack, creating it if necessary."""
    stack = getattr(_tls, "base_dir_stack", None)
    if stack is None:
        stack = []
        _tls.base_dir_stack = stack
    return stack

_MERGE_SENTINEL = object()


def _include_constructor(loader, node):
    """Resolve ``!include <path>`` relative to the file being loaded."""
    rel = loader.construct_scalar(node)
    inc_path = (_get_base_dir_stack()[-1] / rel).resolve()
    return load_yaml(inc_path)


def _include_merge_constructor(loader, node):
    """Return a sentinel-wrapped dict for ``!include-merge`` post-processing."""
    rel = loader.construct_scalar(node)
    inc_path = (_get_base_dir_stack()[-1] / rel).resolve()
    data = load_yaml(inc_path)
    if not isinstance(data, dict):
        raise ValueError(
            f"!include-merge requires a mapping, got {type(data).__name__} from {rel}"
        )
    return (_MERGE_SENTINEL, data)


def _post_process_merges(data):
    """Merge any ``!include-merge`` results into their parent mappings."""
    if not isinstance(data, dict):
        return data

    merges = []
    for key, value in list(data.items()):
        if isinstance(value, tuple) and len(value) == 2 and value[0] is _MERGE_SENTINEL:
            merges.append((key, value[1]))
        else:
            _post_process_merges(value)

    for key, merge_dict in merges:
        del data[key]
        data.update(merge_dict)

    return data


# ---------------------------------------------------------------------------
# Raw (non-resolving) include support for round-trip editing
# ---------------------------------------------------------------------------

class _IncludeTag:
    """Opaque wrapper that preserves ``!include`` / ``!include-merge`` tags
    through a load → modify → save cycle without resolving them."""

    def __init__(self, tag: str, path: str):
        self.tag = tag
        self.path = path


class _PreservingConstructor(RoundTripConstructor):
    """Constructor subclass that keeps include tags as :class:`_IncludeTag`."""


def _preserve_include_tag(loader, node):
    return _IncludeTag(node.tag, loader.construct_scalar(node))

_PreservingConstructor.add_constructor("!include", _preserve_include_tag)
_PreservingConstructor.add_constructor("!include-merge", _preserve_include_tag)


class _PreservingRepresenter(RoundTripRepresenter):
    """Representer subclass that dumps :class:`_IncludeTag` back correctly."""


def _represent_include_tag(dumper, data):
    return dumper.represent_scalar(data.tag, data.path)

_PreservingRepresenter.add_representer(_IncludeTag, _represent_include_tag)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _make_yaml() -> YAML:
    """Create a pre-configured round-trip YAML instance."""
    yml = YAML()
    yml.preserve_quotes = True
    yml.default_flow_style = False
    return yml


def _make_raw_yaml() -> YAML:
    """Create a YAML instance that preserves include tags without resolving."""
    yml = YAML()
    yml.Constructor = _PreservingConstructor
    yml.Representer = _PreservingRepresenter
    yml.preserve_quotes = True
    yml.default_flow_style = False
    return yml


# Register tag constructors once at module level.
_make_yaml().Constructor.add_constructor("!include", _include_constructor)
_make_yaml().Constructor.add_constructor("!include-merge", _include_merge_constructor)


def load_yaml(path: Path | str) -> dict:
    """Load a YAML file, resolving ``!include`` / ``!include-merge`` tags.

    Included paths are resolved relative to the directory containing the
    YAML file being loaded, so nested includes work correctly.

    Returns a ``CommentedMap`` (dict subclass) when the file contains a
    mapping, or a plain ``dict`` otherwise.
    """
    path = Path(path).resolve()
    stack = _get_base_dir_stack()
    stack.append(path.parent)
    try:
        yml = _make_yaml()
        with open(path, "r", encoding="utf-8") as f:
            data = yml.load(f)
        data = data if data is not None else {}
        _post_process_merges(data)
        return data
    finally:
        stack.pop()


def load_yaml_raw(path: Path | str) -> dict:
    """Load a YAML file preserving ``!include`` / ``!include-merge`` as
    opaque :class:`_IncludeTag` objects (no resolution or merging).

    Pair with :func:`save_yaml_raw` for safe round-trip editing of files
    that contain include directives.
    """
    path = Path(path).resolve()
    yml = _make_raw_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yml.load(f)
    return data if data is not None else {}


def save_yaml(data: Any, path: Path | str) -> None:
    """Dump *data* to *path*, preserving any comments attached to the data."""
    path = Path(path)
    yml = _make_yaml()
    with open(path, "w", encoding="utf-8") as f:
        yml.dump(data, f)


def save_yaml_raw(data: Any, path: Path | str) -> None:
    """Dump *data* to *path*, correctly serialising any :class:`_IncludeTag`
    objects back to their original ``!include`` / ``!include-merge`` tags."""
    path = Path(path)
    yml = _make_raw_yaml()
    with open(path, "w", encoding="utf-8") as f:
        yml.dump(data, f)
