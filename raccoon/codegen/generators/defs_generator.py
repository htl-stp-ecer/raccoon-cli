"""Generator for hardware definitions (defs.py)."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import BaseGenerator
from ..builder import build_constructor_expr
from ..class_builder import ClassBuilder
from ..yaml_resolver import create_hardware_resolver

logger = logging.getLogger("raccoon")


class DefsGenerator(BaseGenerator):
    """
    Generator for hardware definitions file (defs.py).

    Generates a class containing hardware component definitions
    (motors, servos, sensors, etc.) from the project configuration.
    """

    def __init__(self, class_name: str = "Defs"):
        """
        Initialize the defs generator.

        Args:
            class_name: Name of the generated class (default: "Defs")
        """
        super().__init__(class_name)
        self.resolver = create_hardware_resolver()

    def get_output_filename(self) -> str:
        """Return the output filename."""
        return "defs.py"

    def extract_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract hardware definitions from config.

        Args:
            config: Full project configuration

        Returns:
            Hardware definitions dictionary
        """
        definitions = config.get("definitions")
        if definitions is None:
            logger.warning("No 'definitions' key found in config")
            return {}
        return definitions

    def validate_config(self, data: Dict[str, Any]) -> None:
        """
        Validate hardware definitions.

        Args:
            data: Hardware definitions to validate

        Raises:
            ValueError: If configuration is invalid
        """
        if not isinstance(data, dict):
            raise ValueError(
                "Top-level config must contain a mapping under key 'definitions:'"
            )

        # Validate each definition entry
        for field_name, hw_cfg in data.items():
            if not isinstance(hw_cfg, dict):
                raise ValueError(f"definitions.{field_name} must be a mapping")

            # Ensure valid identifier
            if not field_name.isidentifier():
                raise ValueError(
                    f"definitions.{field_name}: not a valid Python identifier"
                )

            # Ensure 'type' field exists
            if "type" not in hw_cfg:
                raise ValueError(
                    f"definitions.{field_name}: missing required 'type' field"
                )

    def generate_body(self, data: Dict[str, Any]) -> str:
        """
        Generate the Defs class body.

        Args:
            data: Validated hardware definitions

        Returns:
            Class definition as a string
        """
        if not data:
            # Empty class
            return f"class {self.class_name}:\n    pass"

        # Build class attributes
        attributes = []
        for field_name, hw_cfg in data.items():
            logger.info(f"Processing definition: {field_name}")

            # Resolve type and extract parameters using the unified resolver
            try:
                hw_class, hw_params = self.resolver.resolve_from_config(hw_cfg, type_key="type")
                logger.info(f"Resolved type '{hw_cfg['type']}' to {hw_class.__name__} for {field_name}")
            except ValueError as e:
                raise ValueError(f"definitions.{field_name}: {e}")

            # Build constructor expression (with type checking)
            hw_expr = build_constructor_expr(
                hw_class, hw_params, f"definitions.{field_name}", self.imports
            )
            attributes.append((field_name, hw_expr))

        # Use ClassBuilder to construct the class
        return ClassBuilder.build_simple_class(self.class_name, attributes)
