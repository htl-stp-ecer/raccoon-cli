"""Generator for robot configuration (robot.py)."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import BaseGenerator
from ..builder import build_constructor_expr, build_literal_expr
from ..class_builder import ClassBuilder
from ..introspection import resolve_class
from ..yaml_resolver import create_kinematics_resolver

logger = logging.getLogger("raccoon")


class RobotGenerator(BaseGenerator):
    """
    Generator for robot configuration file (robot.py).

    Generates a Robot class containing kinematics, drive system,
    and other robot-level components configured from the project YAML.
    """

    def __init__(self, class_name: str = "Robot"):
        """
        Initialize the robot generator.

        Args:
            class_name: Name of the generated class (default: "Robot")
        """
        super().__init__(class_name)
        self.kinematics_resolver = create_kinematics_resolver()

    def get_output_filename(self) -> str:
        """Return the output filename."""
        return "robot.py"

    def extract_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract robot configuration.

        Args:
            config: Full project configuration

        Returns:
            Robot configuration dictionary
        """
        robot_config = config.get("robot")
        if robot_config is None:
            logger.warning("No 'robot' key found in config")
            return {}
        return robot_config

    def validate_config(self, data: Dict[str, Any]) -> None:
        """
        Validate robot configuration.

        Args:
            data: Robot configuration to validate

        Raises:
            ValueError: If configuration is invalid
        """
        if not isinstance(data, dict):
            raise ValueError("Robot config must be a mapping under key 'robot:'")

        # Validate kinematics section
        if "kinematics" in data:
            kinematics = data["kinematics"]
            if not isinstance(kinematics, dict):
                raise ValueError("robot.kinematics must be a mapping")

            # Check for required kinematics fields
            if "type" not in kinematics:
                raise ValueError("robot.kinematics.type is required")

    def generate_body(self, data: Dict[str, Any]) -> str:
        """
        Generate the Robot class body.

        Args:
            data: Validated robot configuration

        Returns:
            Class definition as a string
        """
        if not data:
            # Empty Robot class
            return f"class {self.class_name}:\n    pass"

        builder = ClassBuilder(self.class_name)

        # Add import for Defs
        # Note: This will be added to the imports section automatically
        # We'll reference it in the generated code

        # Generate kinematics if present
        if "kinematics" in data:
            kinematics_cfg = data["kinematics"]
            kinematics_expr = self._build_kinematics(kinematics_cfg)
            if kinematics_expr:
                builder.add_class_attribute("kinematics", kinematics_expr)

        # Generate drive system if kinematics exists
        if "kinematics" in data:
            drive_expr = self._build_drive(data)
            if drive_expr:
                builder.add_class_attribute("drive", drive_expr)

        return builder.build()

    # Parameter name mappings (YAML field name -> Python parameter name)
    PARAM_MAPPINGS = {
        "wheelRadius": "wheel_radius",
        "wheelBase": "wheelbase",
        "left_wheel": "left_motor",
        "right_wheel": "right_motor",
    }

    # Parameters that reference hardware definitions
    HARDWARE_REF_PARAMS = {
        "left_wheel", "right_wheel", "left_motor", "right_motor",
        "front_left", "front_right", "back_left", "back_right",
        "front_left_motor", "front_right_motor", "back_left_motor", "back_right_motor",
    }

    def _build_kinematics(self, kinematics_cfg: Dict[str, Any]) -> str:
        """
        Build kinematics constructor expression.

        Args:
            kinematics_cfg: Kinematics configuration

        Returns:
            Constructor expression string
        """
        kinematics_type = kinematics_cfg.get("type", "")
        if not kinematics_type:
            logger.error("Kinematics type is required")
            return ""

        # Resolve the kinematics class using the unified resolver
        try:
            kinematics_class = self.kinematics_resolver.resolve_type(kinematics_type)
            logger.info(f"Resolved kinematics type '{kinematics_type}' to {kinematics_class.__name__}")
        except ValueError as e:
            logger.error(f"Could not resolve kinematics type '{kinematics_type}': {e}")
            return ""

        # Prepare parameters for validation
        # Map YAML field names to actual parameter names and handle hardware references
        params = {}
        for key, value in kinematics_cfg.items():
            if key == "type":
                # Skip the type field itself
                continue

            # Map YAML field names to actual parameter names
            param_name = self.PARAM_MAPPINGS.get(key, key)

            # Check if value is a reference to a hardware definition
            if isinstance(value, str) and (key in self.HARDWARE_REF_PARAMS or param_name in self.HARDWARE_REF_PARAMS):
                # Store as special marker that will be replaced later
                params[param_name] = ("__hardware_ref__", value)
            else:
                # Regular parameter value
                params[param_name] = value

        # Use build_constructor_expr for validation, but we need to handle hardware refs specially
        # First, convert hardware refs to placeholders for validation
        validation_params = {}
        hardware_refs = {}
        for key, value in params.items():
            if isinstance(value, tuple) and value[0] == "__hardware_ref__":
                # For validation purposes, we need to provide a valid object
                # We'll use None as placeholder since we can't access Defs yet
                hardware_refs[key] = value[1]
                # Skip hardware refs in validation - they're always valid Motor objects
                validation_params[key] = None
            else:
                validation_params[key] = value

        # Build constructor with validation (but skip hardware refs in actual validation)
        # We'll do a custom validation for this case
        from ..introspection import get_init_params
        import inspect

        init_params = get_init_params(kinematics_class)
        required_params = {
            name for name, param in init_params.items()
            if param.default == inspect.Parameter.empty
        }

        provided_params = set(params.keys())
        missing_params = required_params - provided_params

        if missing_params:
            raise ValueError(
                f"robot.kinematics: Missing required parameter(s) for {kinematics_class.__name__}: "
                f"{', '.join(sorted(missing_params))}. "
                f"Required: {', '.join(sorted(required_params))}, "
                f"Provided: {', '.join(sorted(provided_params)) if provided_params else 'none'}"
            )

        # Check for unknown parameters
        valid_params = set(init_params.keys())
        unknown_params = provided_params - valid_params

        if unknown_params:
            logger.warning(
                f"robot.kinematics: Unknown parameter(s) for {kinematics_class.__name__}: "
                f"{', '.join(sorted(unknown_params))}. "
                f"Valid parameters: {', '.join(sorted(valid_params))}"
            )

        # Add to imports
        self.imports.add(kinematics_class)

        # Build the final constructor expression
        args = []
        for key in sorted(params.keys()):  # Sort for deterministic output
            value = params[key]
            if isinstance(value, tuple) and value[0] == "__hardware_ref__":
                args.append(f"{key}=Defs.{value[1]}")
            else:
                args.append(f"{key}={build_literal_expr(value)}")

        return f"{kinematics_class.__name__}({', '.join(args)})"

    def _build_drive(self, robot_cfg: Dict[str, Any]) -> str:
        """
        Build drive system constructor expression.

        Args:
            robot_cfg: Full robot configuration

        Returns:
            Constructor expression string
        """
        try:
            drive_class = resolve_class("libstp.drive.Drive")
            self.imports.add(drive_class)
        except (ImportError, AttributeError):
            logger.error("Could not resolve libstp.drive.Drive")
            return ""

        # Drive takes kinematics as primary argument
        return "Drive(kinematics=kinematics)"

    def generate_imports(self) -> str:
        """Generate import statements including Defs import."""
        # Add standard imports
        base_imports = super().generate_imports()

        # Add Defs import - always use src.hardware.defs
        defs_import = "from src.hardware.defs import Defs"

        if base_imports:
            return f"{base_imports}\n\n{defs_import}"
        return defs_import
