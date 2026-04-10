"""Introspection utilities for analyzing Python classes."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("raccoon")


def resolve_class(qualname: str) -> type:
    """Resolve a class name to a ClassProxy via the type index.

    Supports fully qualified names (``libstp.hal.Motor``) and simple
    names (``Motor``) which are looked up in the namespace map.
    """
    from .type_index import get_type_index

    index = get_type_index()

    if "." not in qualname:
        proxy = index.resolve_by_name(qualname)
        if proxy is not None:
            logger.debug(f"Resolved {qualname} via namespace map → {proxy}")
            return proxy  # type: ignore[return-value]
        raise ImportError(f"Cannot resolve simple name '{qualname}'")

    # Exact qualname match
    proxy = index.resolve(qualname)
    if proxy is not None:
        logger.debug(f"Resolved {qualname} from type index")
        return proxy  # type: ignore[return-value]

    # For "libstp.ClassName" lookups, check the namespace map
    _, cls_name = qualname.rsplit(".", 1)
    proxy = index.resolve_by_name(cls_name)
    if proxy is not None:
        logger.debug(f"Resolved {qualname} via namespace map → {proxy}")
        return proxy  # type: ignore[return-value]

    raise ImportError(f"Cannot resolve '{qualname}' from type index")


def qualname_of(cls: type) -> str:
    """Get the fully qualified name of a class."""
    return f"{cls.__module__}.{cls.__name__}"


def get_init_params(cls: type) -> Dict[str, Any]:
    """Get __init__ parameters for a ClassProxy."""
    from .type_index import ClassProxy

    if isinstance(cls, ClassProxy):
        return cls.get_cached_params()

    raise TypeError(f"Expected ClassProxy, got {type(cls).__name__}")


def infer_param_type(parent_cls: type, param_name: str) -> Optional[type]:
    """Infer the type of an __init__ parameter from the type index.

    Returns the resolved ClassProxy for the parameter's type annotation,
    or None if the parameter has no type or it cannot be resolved.
    """
    from .type_index import ClassProxy

    if not isinstance(parent_cls, ClassProxy):
        logger.warning(f"Cannot infer param type: {parent_cls} is not a ClassProxy")
        return None

    type_str = parent_cls.get_param_type(param_name)
    if not type_str:
        logger.warning(f"No type found for parameter '{param_name}' in {parent_cls.__name__}")
        return None

    logger.debug(f"Found type for '{param_name}': {type_str}")

    try:
        resolved = resolve_class(type_str)
        logger.debug(f"Resolved {type_str} to {resolved}")
        return resolved
    except (ImportError, AttributeError):
        pass

    # Try just the class name portion
    class_name = type_str.split('.')[-1]
    if class_name != type_str:
        try:
            resolved = resolve_class(class_name)
            logger.debug(f"Resolved {class_name} to {resolved}")
            return resolved
        except (ImportError, AttributeError):
            pass

    logger.warning(f"Could not resolve type {type_str}")
    return None


# Keep old name as alias for compatibility
parse_type_from_docstring = infer_param_type
