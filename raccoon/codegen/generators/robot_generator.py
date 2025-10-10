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

        # Support both old structure (robot.kinematics) and new structure (robot.drive)
        # If robot.drive exists, use that; otherwise fall back to old structure
        if "drive" in robot_config:
            return robot_config

        # Legacy support: if kinematics is at robot level, wrap it in drive
        if "kinematics" in robot_config:
            logger.info("Using legacy robot.kinematics structure")
            return robot_config

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

        # Validate new structure (robot.drive)
        if "drive" in data:
            drive = data["drive"]
            if not isinstance(drive, dict):
                raise ValueError("robot.drive must be a mapping")

            # Validate kinematics section within drive
            if "kinematics" in drive:
                kinematics = drive["kinematics"]
                if not isinstance(kinematics, dict):
                    raise ValueError("robot.drive.kinematics must be a mapping")

                # Check for required kinematics fields
                if "type" not in kinematics:
                    raise ValueError("robot.drive.kinematics.type is required")

        # Legacy validation: kinematics at robot level
        elif "kinematics" in data:
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

        # Handle new structure: robot.drive
        if "drive" in data:
            drive_cfg = data["drive"]

            # Build kinematics first (needed for drive)
            if "kinematics" in drive_cfg:
                kinematics_cfg = drive_cfg["kinematics"]
                kinematics_expr = self._build_kinematics(kinematics_cfg)
                if kinematics_expr:
                    builder.add_class_attribute("kinematics", kinematics_expr)

                # Build drive using the full drive config
                drive_expr = self._build_drive_from_config(drive_cfg)
                if drive_expr:
                    builder.add_class_attribute("drive", drive_expr)

        # Legacy structure: robot.kinematics at top level
        elif "kinematics" in data:
            kinematics_cfg = data["kinematics"]
            kinematics_expr = self._build_kinematics(kinematics_cfg)
            if kinematics_expr:
                builder.add_class_attribute("kinematics", kinematics_expr)

            # Generate drive system with legacy approach
            drive_expr = self._build_drive_legacy(data)
            if drive_expr:
                builder.add_class_attribute("drive", drive_expr)

        return builder.build()

    # Parameters that reference hardware definitions (motors)
    # These will be converted to Defs.<name> references
    HARDWARE_REF_PARAMS = {
        "left_motor", "right_motor",
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

        # Prepare parameters - use exact names from YAML
        # No automatic mapping - user must provide correct parameter names
        params = {}
        for key, value in kinematics_cfg.items():
            if key == "type":
                # Skip the type field itself
                continue

            # Check if value is a reference to a hardware definition
            if isinstance(value, str) and key in self.HARDWARE_REF_PARAMS:
                # Store as special marker that will be replaced later
                params[key] = ("__hardware_ref__", value)
            else:
                # Regular parameter value
                params[key] = value

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

    def _build_drive_legacy(self, robot_cfg: Dict[str, Any]) -> str:
        """
        Build drive system constructor expression (legacy approach).

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

    def _build_drive_from_config(self, drive_cfg: Dict[str, Any]) -> str:
        """
        Build drive system constructor expression from drive configuration.

        Automatically introspects the Drive class to determine required parameters
        and builds them from the configuration.

        Args:
            drive_cfg: Drive configuration (from robot.drive in YAML)

        Returns:
            Constructor expression string
        """
        try:
            drive_class = resolve_class("libstp.drive.Drive")
            self.imports.add(drive_class)
        except (ImportError, AttributeError):
            logger.error("Could not resolve libstp.drive.Drive")
            return ""

        # Get Drive's __init__ parameters
        from ..introspection import get_init_params
        import inspect

        init_params = get_init_params(drive_class)
        logger.info(f"Drive.__init__ parameters: {list(init_params.keys())}")

        # Build arguments for Drive constructor
        drive_args = []

        for param_name, param in init_params.items():
            if param_name == "kinematics":
                # Kinematics is already built as a class attribute
                drive_args.append("kinematics=kinematics")

            elif param_name == "chassis_lim":
                # Build MotionLimits from limits configuration
                if "limits" in drive_cfg:
                    limits_cfg = drive_cfg["limits"]
                    motion_limits_expr = self._build_motion_limits(limits_cfg)
                    if motion_limits_expr:
                        drive_args.append(f"chassis_lim={motion_limits_expr}")
                    else:
                        # Required parameter missing
                        if param.default == inspect.Parameter.empty:
                            logger.error("robot.drive.limits is required for chassis_lim parameter")
                            return ""
                elif param.default == inspect.Parameter.empty:
                    # Required parameter not in config
                    logger.error(f"Missing required parameter '{param_name}' for Drive constructor")
                    return ""

            else:
                # Handle other parameters if they exist in the config
                if param_name in drive_cfg:
                    from ..builder import build_literal_expr
                    value = drive_cfg[param_name]
                    drive_args.append(f"{param_name}={build_literal_expr(value)}")
                elif param.default == inspect.Parameter.empty:
                    logger.error(f"Missing required parameter '{param_name}' for Drive constructor")
                    return ""

        return f"Drive({', '.join(drive_args)})"

    def _build_motion_limits(self, limits_cfg: Dict[str, Any]) -> str:
        """
        Build MotionLimits constructor expression from limits configuration.

        Args:
            limits_cfg: Limits configuration (from robot.drive.limits in YAML)

        Returns:
            Constructor expression string
        """
        try:
            motion_limits_class = resolve_class("libstp.drive.MotionLimits")
            self.imports.add(motion_limits_class)
        except (ImportError, AttributeError):
            logger.error("Could not resolve libstp.drive.MotionLimits")
            return ""

        # Use exact parameter names from YAML - no automatic mapping
        params = limits_cfg.copy()

        # Get MotionLimits __init__ parameters
        from ..introspection import get_init_params
        from ..builder import build_literal_expr
        import inspect

        init_params = get_init_params(motion_limits_class)
        logger.info(f"MotionLimits.__init__ parameters: {list(init_params.keys())}")

        # Validate required parameters
        required_params = {
            name for name, param in init_params.items()
            if param.default == inspect.Parameter.empty
        }

        provided_params = set(params.keys())
        missing_params = required_params - provided_params

        if missing_params:
            raise ValueError(
                f"robot.drive.limits: Missing required parameter(s) for MotionLimits: "
                f"{', '.join(sorted(missing_params))}. "
                f"Required: {', '.join(sorted(required_params))}, "
                f"Provided: {', '.join(sorted(provided_params)) if provided_params else 'none'}"
            )

        # Check for unknown parameters
        valid_params = set(init_params.keys())
        unknown_params = provided_params - valid_params

        if unknown_params:
            raise ValueError(
                f"robot.drive.limits: Unknown parameter(s) for MotionLimits: "
                f"{', '.join(sorted(unknown_params))}. "
                f"Valid parameters: {', '.join(sorted(valid_params))}"
            )

        # Build constructor arguments
        args = []
        for key in sorted(params.keys()):
            value = params[key]
            args.append(f"{key}={build_literal_expr(value)}")

        return f"MotionLimits({', '.join(args)})"

    def generate_imports(self) -> str:
        """Generate import statements including Defs import."""
        # Add standard imports
        base_imports = super().generate_imports()

        # Add Defs import - always use src.hardware.defs
        defs_import = "from src.hardware.defs import Defs"

        if base_imports:
            return f"{base_imports}\n\n{defs_import}"
        return defs_import
