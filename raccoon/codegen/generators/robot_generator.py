"""Generator for robot configuration (robot.py)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseGenerator
from ..builder import build_constructor_expr, build_literal_expr
from ..class_builder import ClassBuilder
from ..introspection import resolve_class
from ..yaml_resolver import create_kinematics_resolver, create_odometry_resolver

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
        self.odometry_resolver = create_odometry_resolver()
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

        # Validate odometry structure if present
        if "odometry" in data:
            odometry_cfg = data["odometry"]
            if not isinstance(odometry_cfg, (str, dict)):
                raise ValueError("robot.odometry must be a string or a mapping")
            if isinstance(odometry_cfg, dict) and "type" not in odometry_cfg:
                raise ValueError("robot.odometry.type is required when using mapping form")

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

    def write(self, config: Dict[str, Any], output_dir: Path, format_code: bool = True) -> Path:
        """
        Generate and write file to disk.

        Overridden to store full config for hardware reference validation.
        """
        self._full_config = config
        return super().write(config, output_dir, format_code)

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

        kinematics_expr = ""
        drive_expr = ""

        # Handle new structure: robot.drive
        if "drive" in data:
            drive_cfg = data["drive"]

            # Build kinematics first (needed for drive)
            if "kinematics" in drive_cfg:
                kinematics_cfg = drive_cfg["kinematics"]
                kinematics_expr = self._build_kinematics(kinematics_cfg)

                # Build drive using the full drive config
            drive_expr = self._build_drive_from_config(drive_cfg)

        # Legacy structure: robot.kinematics at top level
        elif "kinematics" in data:
            kinematics_cfg = data["kinematics"]
            kinematics_expr = self._build_kinematics(kinematics_cfg)

            # Generate drive system with legacy approach
            drive_expr = self._build_drive_legacy(data)

        if kinematics_expr:
            builder.add_class_attribute("kinematics", kinematics_expr)

        if drive_expr:
            builder.add_class_attribute("drive", drive_expr)

        odometry_expr = self._build_odometry(
            data.get("odometry"),
            has_kinematics=bool(kinematics_expr),
            has_drive=bool(drive_expr),
        )
        if odometry_expr:
            builder.add_class_attribute("odometry", odometry_expr)

        # Add motion_pid config if present
        motion_pid_expr = self._build_motion_pid_config(data.get("motion_pid"))
        if motion_pid_expr:
            builder.add_class_attribute("motion_pid_config", motion_pid_expr)

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
            formatted_instances = mission_instances.replace(", ", ",\n        ")
            builder.add_class_attribute(
                "missions",
                f"[\n        {formatted_instances}\n    ]",
            )

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

    ODOMETRY_PARAM_HINTS = {
        # IMU-only odometry
        "libstp.odometry_imu.ImuOdometry": ["imu", "kinematics"],
        "ImuOdometry": ["imu", "kinematics"],
        # Fused odometry combines IMU + kinematics with invert flags
        "libstp.odometry_fused.FusedOdometry": [
            "imu",
            "kinematics",
            "invert_x",
            "invert_y",
            "invert_z",
            "invert_w",
        ],
        "FusedOdometry": [
            "imu",
            "kinematics",
            "invert_x",
            "invert_y",
            "invert_z",
            "invert_w",
        ],
    }

    def _build_odometry(
        self,
        odometry_cfg: Any,
        *,
        has_kinematics: bool,
        has_drive: bool,
    ) -> str:
        """
        Build odometry constructor expression.

        Args:
            odometry_cfg: Odometry configuration (string or mapping)
            has_kinematics: Whether a kinematics attribute will be generated
            has_drive: Whether a drive attribute will be generated

        Returns:
            Constructor expression string
        """
        if odometry_cfg is None:
            return ""

        if isinstance(odometry_cfg, str):
            odometry_type = odometry_cfg
            params_cfg: Dict[str, Any] = {}
        elif isinstance(odometry_cfg, dict):
            odometry_type = odometry_cfg.get("type", "")
            params_cfg = {k: v for k, v in odometry_cfg.items() if k != "type"}
        else:
            raise ValueError("robot.odometry must be a string or mapping")

        if not odometry_type:
            logger.error("robot.odometry.type is required to generate odometry")
            return ""

        qualified_name = self._resolve_odometry_qualified_name(odometry_type)

        try:
            odometry_class = self.odometry_resolver.resolve_type(odometry_type)
            logger.info(
                f"Resolved odometry type '{odometry_type}' to {odometry_class.__name__}"
            )
            init_params = self._introspect_odometry_params(odometry_class)
        except ValueError as e:
            logger.warning(
                "robot.odometry: %s. Falling back to heuristic parameter mapping "
                "for '%s'. Ensure libstp is available during generation for full validation.",
                e,
                qualified_name,
            )
            odometry_class = self._create_placeholder_class(qualified_name)
            init_params = self._odometry_param_hints(qualified_name)
            if not init_params:
                raise ValueError(
                    f"robot.odometry: Unable to determine parameters for '{qualified_name}'. "
                    "Install libstp to enable introspection or provide parameter hints."
                ) from e

        self.imports.add(odometry_class)

        import inspect

        definitions = {}
        if hasattr(self, "_full_config"):
            definitions = self._full_config.get("definitions", {}) or {}
        definition_names = set(definitions.keys())

        reference_map = {
            "imu": "defs.imu",
            "defs": "defs",
        }
        if has_kinematics:
            reference_map["kinematics"] = "kinematics"
        if has_drive:
            reference_map["drive"] = "drive"
        for name in definition_names:
            reference_map[name] = f"defs.{name}"

        param_exprs: Dict[str, str] = {}
        used_config_keys: set[str] = set()

        for name, param in init_params.items():
            if name in params_cfg:
                param_exprs[name] = self._render_odometry_param_value(
                    params_cfg[name],
                    reference_map,
                )
                used_config_keys.add(name)
            elif name in reference_map:
                ref_expr = reference_map[name]
                if ref_expr == "kinematics" and not has_kinematics:
                    raise ValueError(
                        f"robot.odometry: '{odometry_class.__name__}' requires a kinematics attribute"
                    )
                if ref_expr == "drive" and not has_drive:
                    raise ValueError(
                        f"robot.odometry: '{odometry_class.__name__}' requires a drive attribute"
                    )
                param_exprs[name] = ref_expr
            elif param.default == inspect.Parameter.empty:
                raise ValueError(
                    f"robot.odometry: Missing required parameter '{name}' for {odometry_class.__name__}"
                )

        # Warn about unused configuration keys
        unused_keys = set(params_cfg.keys()) - used_config_keys
        for key in sorted(unused_keys):
            if key not in init_params:
                logger.warning(
                    f"robot.odometry: Unknown parameter '{key}' for {odometry_class.__name__}"
                )

        args = []
        for name in init_params.keys():
            if name in param_exprs:
                args.append(f"{name}={param_exprs[name]}")

        # Append optional parameters supplied in config but not part of the signature ordering
        # (e.g., when odometry __init__ accepts **kwargs in the binding).
        extra_keys = [
            key for key in used_config_keys if key not in init_params.keys()
        ]
        for key in extra_keys:
            args.append(
                f"{key}={self._render_odometry_param_value(params_cfg[key], reference_map)}"
            )

        return f"{odometry_class.__name__}({', '.join(args)})"

    def _render_odometry_param_value(
        self,
        value: Any,
        reference_map: Dict[str, str],
    ) -> str:
        """
        Convert an odometry parameter value to a Python expression.

        Args:
            value: Raw value from configuration
            reference_map: Mapping of known reference names to expressions

        Returns:
            Python expression string
        """
        if isinstance(value, str):
            if value in reference_map:
                return reference_map[value]
            if value.startswith("defs.") and value[5:] in reference_map:
                return reference_map[value[5:]]
        return build_literal_expr(value)

    def _resolve_odometry_qualified_name(self, odometry_type: str) -> str:
        """
        Determine the qualified name for the odometry class without requiring import.
        """
        if "." in odometry_type:
            return odometry_type

        lookup = self.odometry_resolver.type_lookup.get(odometry_type.lower())
        if lookup:
            return lookup

        # Assume the type is declared in libstp.odometry.<lowercase> by convention
        module_name = f"libstp.odometry_{odometry_type.lower()}"
        return f"{module_name}.{odometry_type}"

    def _create_placeholder_class(self, qualified_name: str) -> type:
        """
        Create a lightweight stand-in class for import emission when actual type is unavailable.
        """
        module_name, class_name = qualified_name.rsplit(".", 1)
        return type(class_name, (), {"__module__": module_name})

    def _introspect_odometry_params(self, odometry_class: type) -> Dict[str, Any]:
        """
        Introspect odometry __init__ parameters using available bindings.
        """
        from ..introspection import get_init_params

        return get_init_params(odometry_class)

    def _odometry_param_hints(self, qualified_name: str) -> Dict[str, Any]:
        """
        Provide parameter hints for known odometry classes when introspection is unavailable.
        """
        import inspect

        hints: Optional[List[str]] = None
        if qualified_name in self.ODOMETRY_PARAM_HINTS:
            hints = self.ODOMETRY_PARAM_HINTS[qualified_name]
        else:
            class_name = qualified_name.rsplit(".", 1)[-1]
            hints = self.ODOMETRY_PARAM_HINTS.get(class_name)

        if not hints:
            return {}

        return {
            name: inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=inspect.Parameter.empty,
            )
            for name in hints
        }

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

    def _build_motion_pid_config(self, motion_pid_cfg: Dict[str, Any] | None) -> str:
        """
        Build UnifiedMotionPidConfig constructor expression from motion_pid configuration.

        Args:
            motion_pid_cfg: Motion PID configuration (from robot.motion_pid in YAML)

        Returns:
            Constructor expression string, or empty string if no config
        """
        if not motion_pid_cfg:
            return ""

        try:
            motion_pid_class = resolve_class("libstp.motion.UnifiedMotionPidConfig")
            self.imports.add(motion_pid_class)
        except (ImportError, AttributeError):
            logger.warning("Could not resolve libstp.motion.UnifiedMotionPidConfig - skipping motion_pid config generation")
            return ""

        from ..builder import build_literal_expr

        # Flatten the nested config structure to match UnifiedMotionPidConfig constructor parameters
        params = {}

        # Distance PID gains
        if "distance" in motion_pid_cfg:
            distance_cfg = motion_pid_cfg["distance"]
            if "kp" in distance_cfg:
                params["distance_kp"] = distance_cfg["kp"]
            if "ki" in distance_cfg:
                params["distance_ki"] = distance_cfg["ki"]
            if "kd" in distance_cfg:
                params["distance_kd"] = distance_cfg["kd"]

        # Heading PID gains
        if "heading" in motion_pid_cfg:
            heading_cfg = motion_pid_cfg["heading"]
            if "kp" in heading_cfg:
                params["heading_kp"] = heading_cfg["kp"]
            if "ki" in heading_cfg:
                params["heading_ki"] = heading_cfg["ki"]
            if "kd" in heading_cfg:
                params["heading_kd"] = heading_cfg["kd"]

        # Lateral PID gains
        if "lateral" in motion_pid_cfg:
            lateral_cfg = motion_pid_cfg["lateral"]
            if "kp" in lateral_cfg:
                params["lateral_kp"] = lateral_cfg["kp"]
            if "ki" in lateral_cfg:
                params["lateral_ki"] = lateral_cfg["ki"]
            if "kd" in lateral_cfg:
                params["lateral_kd"] = lateral_cfg["kd"]

        # Profile parameters
        if "profile" in motion_pid_cfg:
            profile_cfg = motion_pid_cfg["profile"]
            if "max_linear_acceleration" in profile_cfg:
                params["max_linear_acceleration"] = profile_cfg["max_linear_acceleration"]
            if "max_angular_acceleration" in profile_cfg:
                params["max_angular_acceleration"] = profile_cfg["max_angular_acceleration"]

        # Saturation handling
        if "saturation" in motion_pid_cfg:
            saturation_cfg = motion_pid_cfg["saturation"]
            if "derating_factor" in saturation_cfg:
                params["saturation_derating_factor"] = saturation_cfg["derating_factor"]
            if "min_scale" in saturation_cfg:
                params["saturation_min_scale"] = saturation_cfg["min_scale"]
            if "recovery_rate" in saturation_cfg:
                params["saturation_recovery_rate"] = saturation_cfg["recovery_rate"]

        # Heading-specific saturation handling
        if "heading_saturation" in motion_pid_cfg:
            heading_sat_cfg = motion_pid_cfg["heading_saturation"]
            if "derating_factor" in heading_sat_cfg:
                params["heading_saturation_derating_factor"] = heading_sat_cfg["derating_factor"]
            if "min_scale" in heading_sat_cfg:
                params["heading_saturation_min_scale"] = heading_sat_cfg["min_scale"]
            if "recovery_rate" in heading_sat_cfg:
                params["heading_saturation_recovery_rate"] = heading_sat_cfg["recovery_rate"]

        # Tolerances
        if "tolerances" in motion_pid_cfg:
            tolerances_cfg = motion_pid_cfg["tolerances"]
            if "distance_m" in tolerances_cfg:
                params["tolerance_distance_m"] = tolerances_cfg["distance_m"]
            if "angle_rad" in tolerances_cfg:
                params["tolerance_angle_rad"] = tolerances_cfg["angle_rad"]

        # Rate limits
        if "rate_limits" in motion_pid_cfg:
            rate_limits_cfg = motion_pid_cfg["rate_limits"]
            if "max_heading_rate" in rate_limits_cfg:
                params["max_heading_rate"] = rate_limits_cfg["max_heading_rate"]
            if "min_angular_rate" in rate_limits_cfg:
                params["min_angular_rate"] = rate_limits_cfg["min_angular_rate"]

        # Lateral drift handling
        if "lateral_drift" in motion_pid_cfg:
            lateral_drift_cfg = motion_pid_cfg["lateral_drift"]
            if "heading_bias_gain" in lateral_drift_cfg:
                params["lateral_heading_bias_gain"] = lateral_drift_cfg["heading_bias_gain"]
            if "reorient_threshold_m" in lateral_drift_cfg:
                params["lateral_reorient_threshold_m"] = lateral_drift_cfg["reorient_threshold_m"]
            if "heading_saturation_error_rad" in lateral_drift_cfg:
                params["lateral_heading_saturation_error_rad"] = lateral_drift_cfg["heading_saturation_error_rad"]
            if "heading_recovery_error_rad" in lateral_drift_cfg:
                params["lateral_heading_recovery_error_rad"] = lateral_drift_cfg["heading_recovery_error_rad"]

        # Top-level parameters
        top_level_params = [
            "integral_max",
            "integral_deadband",
            "derivative_lpf_alpha",
            "output_min",
            "output_max",
            "min_speed_mps"
        ]
        for param in top_level_params:
            if param in motion_pid_cfg:
                params[param] = motion_pid_cfg[param]

        # Build constructor arguments
        if not params:
            return ""

        args = []
        for key in sorted(params.keys()):
            value = params[key]
            args.append(f"{key}={build_literal_expr(value)}")

        return f"UnifiedMotionPidConfig({', '.join(args)})"

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
