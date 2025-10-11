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
        self.mission_imports = []  # Store mission imports separately

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

    def generate(self, config: Dict[str, Any]) -> str:
        """
        Generate the complete file content.

        Overridden to pass full config to generate_body for mission processing.

        Args:
            config: Full project configuration

        Returns:
            Complete file content as a string
        """
        # Store full config for use in generate_body and validation
        self._full_config = config

        # Call parent generate
        return super().generate(config)

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

        builder = ClassBuilder(self.class_name, base_classes=["GenericRobot"])

        # Add Defs instance as first class attribute
        builder.add_class_attribute("defs", "Defs()")

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

        # Add missions from full config
        if hasattr(self, '_full_config'):
            self._add_missions_to_builder(builder, self._full_config)

        return builder.build()

    def _add_missions_to_builder(self, builder: ClassBuilder, config: Dict[str, Any]) -> None:
        """
        Add mission attributes to the class builder.

        Args:
            builder: The ClassBuilder instance to add missions to
            config: Full project configuration
        """
        missions = config.get("missions", [])
        if not missions:
            return

        normal_missions = []
        setup_mission = None
        shutdown_mission = None

        for mission_entry in missions:
            # Handle both string format and dict format
            if isinstance(mission_entry, str):
                mission_name = mission_entry
                mission_type = "normal"
            elif isinstance(mission_entry, dict):
                mission_name = list(mission_entry.keys())[0]
                mission_type = mission_entry[mission_name]
            else:
                logger.warning(f"Skipping invalid mission entry: {mission_entry}")
                continue

            # Convert mission name to snake_case for filename
            mission_file = self._class_name_to_snake_case(mission_name)

            # Store mission import
            self.mission_imports.append({
                "class_name": mission_name,
                "file_name": mission_file,
                "type": mission_type
            })

            # Categorize missions
            if mission_type == "setup":
                setup_mission = mission_name
            elif mission_type == "shutdown":
                shutdown_mission = mission_name
            else:
                normal_missions.append(mission_name)

        # Add missions list
        if normal_missions:
            mission_instances = ", ".join([f"{name}()" for name in normal_missions])
            builder.add_class_attribute("missions", f"[\n        {mission_instances.replace(', ', ',\n        ')}\n    ]")

        # Add setup mission
        if setup_mission:
            builder.add_class_attribute("setup_mission", f"{setup_mission}()")
        else:
            builder.add_class_attribute("setup_mission", "None")

        # Add shutdown mission
        if shutdown_mission:
            builder.add_class_attribute("shutdown_mission", f"{shutdown_mission}()")
        else:
            builder.add_class_attribute("shutdown_mission", "None")

    @staticmethod
    def _class_name_to_snake_case(class_name: str) -> str:
        """
        Convert a class name to snake_case for filename.

        Args:
            class_name: The class name (e.g., "DriveToPotatoMission")

        Returns:
            Snake case filename (e.g., "drive_to_potato_mission")
        """
        import re
        # Insert underscore before uppercase letters (except at start)
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', class_name)
        # Insert underscore before uppercase letters preceded by lowercase
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    # Parameters that reference hardware definitions (motors)
    # These will be converted to Defs.<name> references
    HARDWARE_REF_PARAMS = {
        "left_motor", "right_motor",
        "front_left_motor", "front_right_motor", "back_left_motor", "back_right_motor",
        "rear_left_motor", "rear_right_motor",
    }

    def _validate_hardware_refs(self, hardware_refs: Dict[str, str]) -> None:
        """
        Validate that all hardware references exist in definitions.

        Args:
            hardware_refs: Dictionary mapping parameter names to hardware definition names

        Raises:
            ValueError: If any hardware reference doesn't exist in definitions
        """
        if not hardware_refs:
            return

        # Get definitions from full config
        if not hasattr(self, '_full_config'):
            logger.warning("Cannot validate hardware references: full config not available")
            return

        definitions = self._full_config.get("definitions", {})
        if not definitions:
            raise ValueError(
                "No hardware definitions found in config, but kinematics references hardware: "
                f"{', '.join(hardware_refs.values())}"
            )

        # Check each hardware reference
        missing_refs = []
        for param_name, def_name in hardware_refs.items():
            if def_name not in definitions:
                missing_refs.append(f"{param_name}='{def_name}'")

        if missing_refs:
            available_defs = ', '.join(sorted(definitions.keys()))
            raise ValueError(
                f"robot.drive.kinematics: Hardware reference(s) not found in definitions: "
                f"{', '.join(missing_refs)}. "
                f"Available definitions: {available_defs}"
            )

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

        # Validate that all hardware references exist in definitions
        self._validate_hardware_refs(hardware_refs)

        # Add to imports
        self.imports.add(kinematics_class)

        # Build the final constructor expression
        args = []
        for key in sorted(params.keys()):  # Sort for deterministic output
            value = params[key]
            if isinstance(value, tuple) and value[0] == "__hardware_ref__":
                args.append(f"{key}=defs.{value[1]}")
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
        """Generate import statements including Defs, GenericRobot, and mission imports."""
        # Add standard imports
        base_imports = super().generate_imports()

        # Add GenericRobot import
        generic_robot_import = "from libstp.robot.api import GenericRobot"

        # Add Defs import - always use src.hardware.defs
        defs_import = "from src.hardware.defs import Defs"

        # Build mission imports
        mission_import_lines = []
        if self.mission_imports:
            for mission in self.mission_imports:
                class_name = mission["class_name"]
                file_name = mission["file_name"]
                # Use import from src.missions (project root is in sys.path)
                mission_import_lines.append(
                    f"from src.missions.{file_name} import {class_name}"
                )

        # Combine all imports
        parts = []
        if base_imports:
            parts.append(base_imports)

        parts.append(generic_robot_import)
        parts.append(defs_import)

        if mission_import_lines:
            parts.append("\n" + "\n".join(mission_import_lines))

        return "\n\n".join(parts)
