"""Round-trip YAML utilities that preserve comments and formatting."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, CommentedMap, CommentedSeq

# ---------------------------------------------------------------------------
# !include / !include-merge support
# ---------------------------------------------------------------------------

# Thread-local stack of base directories for resolving relative !include paths.
_tls = threading.local()
_MERGE_SENTINEL = object()

def safe_for_yaml(node):
    """Recursively make node safe for ruamel.yaml while preserving comments."""
    if isinstance(node, CommentedMap):
        for k, v in node.items():
            node[k] = safe_for_yaml(v)
        return node
    elif isinstance(node, CommentedSeq):
        for i, v in enumerate(node):
            node[i] = safe_for_yaml(v)
        return node
    elif isinstance(node, (str, int, float, bool, type(None))):
        return node
    else:
        # Only convert unsupported objects to string
        return str(node)

@contextmanager
def _base_dir_context(base: Path):
    _get_base_dir_stack().append(base)
    try:
        yield
    finally:
        _get_base_dir_stack().pop()


def _get_base_dir_stack() -> list[Path]:
    stack = getattr(_tls, "base_dir_stack", None)
    if stack is None:
        stack = []
        _tls.base_dir_stack = stack
    return stack


def _include_constructor(loader, node):
    rel = loader.construct_scalar(node)
    inc_path = (_get_base_dir_stack()[-1] / rel).resolve()
    return load_yaml(inc_path)


def _include_merge_constructor(loader, node):
    rel = loader.construct_scalar(node)
    inc_path = (_get_base_dir_stack()[-1] / rel).resolve()
    data = load_yaml(inc_path)
    if not isinstance(data, dict):
        raise ValueError(f"!include-merge requires a mapping, got {type(data).__name__} from {rel}")
    return (_MERGE_SENTINEL, data)


def _post_process_merges(data):
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
# Core helpers
# ---------------------------------------------------------------------------

def _make_yaml() -> YAML:
    yml = YAML()
    yml.preserve_quotes = True
    yml.default_flow_style = False
    return yml


def _make_yaml_no_comments() -> YAML:
    yml = YAML()
    yml.preserve_quotes = False
    yml.default_flow_style = False
    return yml

# Register tag constructors once at module level.
_make_yaml().Constructor.add_constructor("!include", _include_constructor)
_make_yaml().Constructor.add_constructor("!include-merge", _include_merge_constructor)


@contextmanager
def push_base_dir(path: Path):
    stack = _get_base_dir_stack()
    stack.append(path.parent)
    try:
        yield
    finally:
        stack.pop()



def _pop_base_dir() -> None:
    stack = _get_base_dir_stack()
    if not stack:
        raise RuntimeError("Attempted to pop empty YAML base-dir stack")
    stack.pop()


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


def save_yaml(data: Any, path: Path | str) -> None:
    """Dump *data* to *path*, preserving any comments attached to the data."""
    path = Path(path)
    yml = _make_yaml()
    with open(path, "w", encoding="utf-8") as f:
        yml.dump(data, f)


def load_yaml_no_comments(path: Path | str) -> dict:
    """Load a YAML file while ignoring comments during parsing.
    
    Useful for wizard operations that want to ignore existing comments
    and work with clean data.
    
    Still resolves ``!include`` / ``!include-merge`` tags like load_yaml().
    """
    path = Path(path).resolve()
    stack = _get_base_dir_stack()
    stack.append(path.parent)
    try:
        yml = _make_yaml_no_comments()
        with open(path, "r", encoding="utf-8") as f:
            data = yml.load(f)
        data = data if data is not None else {}
        _post_process_merges(data)
        return data
    finally:
        stack.pop()


