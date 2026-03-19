"""Unified YAML type resolver for code generation."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .introspection import resolve_class

logger = logging.getLogger("raccoon")


class YamlResolver:
    """
    Unified resolver for types and values from YAML configuration.

    This class provides a consistent way to resolve hardware types,
    kinematics types, and other class references from YAML config,
    with support for both simple names and fully qualified names.
    """

    def __init__(self, default_namespaces: List[str] | None = None):
        """
        Initialize the resolver.

        Args:
            default_namespaces: List of default namespaces to search for simple type names
        """
        self.default_namespaces = default_namespaces or []
        self.type_lookup: Dict[str, str] = {}

    def add_namespace(self, namespace: str) -> None:
        """
        Add a namespace to search for simple type names.

        Args:
            namespace: Fully qualified namespace (e.g., "libstp.hal")
        """
        if namespace not in self.default_namespaces:
            self.default_namespaces.append(namespace)

    def add_type_mapping(self, simple_name: str, qualified_name: str) -> None:
        """
        Add a type mapping for quick lookup.

        Args:
            simple_name: Simple name (e.g., "differential")
            qualified_name: Fully qualified name (e.g., "libstp.kinematics_differential.DifferentialKinematics")
        """
        self.type_lookup[simple_name.lower()] = qualified_name

    def resolve_type(self, type_name: str) -> type:
        """
        Resolve a type name to a class object.

        Supports three formats:
        1. Fully qualified names (e.g., "libstp.hal.Motor")
        2. Simple names with lookup table (e.g., "differential" -> "libstp.kinematics_differential.DifferentialKinematics")
        3. Simple names with namespace search (e.g., "Motor" searches in default_namespaces)

        Args:
            type_name: Type name to resolve

        Returns:
            Resolved class object

        Raises:
            ValueError: If type cannot be resolved
        """
        # Check if it's a fully qualified name (contains a dot)
        if "." in type_name:
            try:
                cls = resolve_class(type_name)
                logger.debug(f"Resolved fully qualified type '{type_name}'")
                return cls
            except (ImportError, AttributeError) as e:
                raise ValueError(f"Could not resolve fully qualified type '{type_name}': {e}")

        # Try lookup table for known simple names
        simple_name = type_name.lower()
        if simple_name in self.type_lookup:
            qualified_name = self.type_lookup[simple_name]
            try:
                cls = resolve_class(qualified_name)
                logger.debug(f"Resolved '{type_name}' from lookup table to {qualified_name}")
                return cls
            except (ImportError, AttributeError) as e:
                logger.warning(f"Lookup table entry for '{simple_name}' failed to resolve: {e}")

        # Try simple name in default namespaces
        if self.default_namespaces:
            for namespace in self.default_namespaces:
                qualified_name = f"{namespace}.{type_name}"
                try:
                    cls = resolve_class(qualified_name)
                    logger.debug(f"Resolved '{type_name}' to {qualified_name}")
                    return cls
                except (ImportError, AttributeError):
                    continue

        # Build error message
        tried = []
        if simple_name in self.type_lookup:
            tried.append(f"lookup: {self.type_lookup[simple_name]}")
        tried.extend(f"{ns}.{type_name}" for ns in self.default_namespaces)

        raise ValueError(
            f"Could not resolve type '{type_name}'. Tried: {', '.join(tried) if tried else 'no namespaces configured'}"
        )

    def resolve_from_config(self, config: Dict[str, Any], type_key: str = "type") -> tuple[type, Dict[str, Any]]:
        """
        Resolve a type from a YAML config dictionary and extract remaining parameters.

        Args:
            config: Configuration dictionary containing a type field
            type_key: Key name for the type field (default: "type")

        Returns:
            Tuple of (resolved class, parameters dict without type key)

        Raises:
            ValueError: If type field is missing or cannot be resolved
        """
        if type_key not in config:
            raise ValueError(f"Missing required '{type_key}' field in config")

        type_name = config[type_key]
        cls = self.resolve_type(type_name)

        # Remove type key from params
        params = {k: v for k, v in config.items() if k != type_key}

        return cls, params


# Create standard resolvers for common use cases

def create_hardware_resolver() -> YamlResolver:
    """Create a resolver configured for hardware types."""
    resolver = YamlResolver(default_namespaces=["libstp", "libstp.hal", "libstp.foundation", "libstp.imu"])
    return resolver


def create_kinematics_resolver() -> YamlResolver:
    """Create a resolver configured for kinematics types."""
    resolver = YamlResolver()
    resolver.add_type_mapping("differential", "libstp.kinematics_differential.DifferentialKinematics")
    resolver.add_type_mapping("mecanum", "libstp.kinematics_mecanum.MecanumKinematics")
    return resolver


def create_odometry_resolver() -> YamlResolver:
    """Create a resolver configured for odometry types."""
    resolver = YamlResolver(default_namespaces=["libstp.odometry", "libstp"])
    resolver.add_type_mapping("imuodometry", "libstp.odometry_imu.ImuOdometry")
    resolver.add_type_mapping("imu", "libstp.odometry_imu.ImuOdometry")
    resolver.add_type_mapping("fusedodometry", "libstp.odometry_fused.FusedOdometry")
    resolver.add_type_mapping("fused", "libstp.odometry_fused.FusedOdometry")
    return resolver
