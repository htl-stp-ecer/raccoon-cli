"""Generator for hardware definitions (defs.py)."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import BaseGenerator
from ..builder import build_constructor_expr
from ..class_builder import ClassBuilder
from ..introspection import resolve_class
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
        self._imu_import_line: str | None = None

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
        self._ensure_imu_import()

        # Build class attributes
        attributes = [("imu", "Imu()")]
        for field_name, hw_cfg in data.items():
            if field_name == "imu":
                logger.info(
                    "definitions.imu is generated automatically; ignoring configuration entry"
                )
                continue

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

    def _ensure_imu_import(self) -> None:
        """Resolve the appropriate import line for the Imu definition."""
        if self._imu_import_line is not None:
            return

        candidates = [
            ("from libstp.imu import Imu", "libstp.imu.Imu"),
            ("from libstp.hal import IMU as Imu", "libstp.hal.IMU"),
        ]

        for import_line, qualname in candidates:
            try:
                resolve_class(qualname)
                self._imu_import_line = import_line
                logger.debug(f"Using '{import_line}' for Imu definition")
                return
            except (ImportError, AttributeError):
                continue

        # Fall back to legacy HAL import so generation still succeeds.
        self._imu_import_line = "from libstp.hal import IMU as Imu"
        logger.warning(
            "Could not import libstp.imu.Imu or libstp.hal.IMU during generation. "
            "Defaulting to 'from libstp.hal import IMU as Imu'; ensure the target "
            "environment provides a compatible IMU class."
        )

    def generate_imports(self) -> str:
        """Generate import statements, ensuring Imu is always imported."""
        base_imports = super().generate_imports()
        self._ensure_imu_import()

        parts = []
        if base_imports:
            parts.append(base_imports)
        if self._imu_import_line:
            parts.append(self._imu_import_line)

        return "\n".join(parts)
