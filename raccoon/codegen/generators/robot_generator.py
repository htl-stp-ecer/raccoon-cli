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
            Robot configuration dictionary (includes missions for cache fingerprinting)
        """
        robot_config = config.get("robot")
        if robot_config is None:
            logger.warning("No 'robot' key found in config")
            return {}

        # Copy so we can add missions without mutating the original
        result = dict(robot_config)

        # Include missions and definitions in extracted config so the cache
        # fingerprint accounts for changes (these live at config root, not under robot)
        missions = config.get("missions", [])
        if missions:
            result["_missions"] = missions
        definitions = config.get("definitions", {})
        if definitions:
            result["_definitions"] = definitions

        # Support both old structure (robot.kinematics) and new structure (robot.drive)
        if "drive" in result:
            return result

        # Legacy support: if kinematics is at robot level
        if "kinematics" in result:
            logger.info("Using legacy robot.kinematics structure")
            return result

        return result

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

        # Unified motion PID config is required
        if "motion_pid" not in data:
            raise ValueError(
                "robot.motion_pid is required. Please add a 'motion_pid' block under 'robot'."
            )
        if not isinstance(data["motion_pid"], dict):
            raise ValueError("robot.motion_pid must be a mapping")

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

        self._needs_vel_config_helper = False
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

        # Add motion_pid config (required) – uses build_constructor_expr for
        # fully dynamic type detection with nested types, no hardcoded params.
        motion_pid_cfg = data.get("motion_pid")
        if motion_pid_cfg is not None:
            try:
                motion_pid_class = resolve_class("libstp.motion.UnifiedMotionPidConfig")
                self.imports.add(motion_pid_class)
                motion_pid_expr = build_constructor_expr(
                    motion_pid_class, motion_pid_cfg, "robot.motion_pid", self.imports
                )
                builder.add_class_attribute("motion_pid_config", motion_pid_expr)
            except (ImportError, AttributeError):
                logger.warning(
                    "Could not resolve libstp.motion.UnifiedMotionPidConfig"
                    " - skipping motion_pid config generation"
                )

        # Add shutdown_in (required by GenericRobot)
        shutdown_in = data.get("shutdown_in")
        if shutdown_in is None:
            raise ValueError(
                "Missing required 'shutdown_in' in raccoon.project.yml"
            )
        builder.add_class_attribute("shutdown_in", repr(shutdown_in))

        # Add missions from full config
        if hasattr(self, '_full_config'):
            self._add_missions_to_builder(builder, self._full_config)

        # Add geometry configuration from robot.physical
        if hasattr(self, '_full_config'):
            self._add_geometry_to_builder(builder, self._full_config)

        parts = []

        # Add helper function for ChassisVelocityControlConfig if needed
        if self._needs_vel_config_helper:
            parts.append(self._generate_vel_config_helper())
            parts.append("")

        parts.append(builder.build())
        return "\n".join(parts)

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

    def _add_geometry_to_builder(self, builder: ClassBuilder, config: Dict[str, Any]) -> None:
        """
        Add robot geometry properties to the class builder.

        Generates class attributes for:
        - Physical dimensions (width_cm, length_cm)
        - Rotation center offset
        - Sensor positions (keyed by defs.sensor_name)
        - Wheel positions (from kinematics)

        Args:
            builder: The ClassBuilder instance to add geometry to
            config: Full project configuration
        """
        robot_config = config.get("robot", {})
        physical = robot_config.get("physical", {})

        if not physical:
            return

        # Add physical dimensions
        width_cm = physical.get("width_cm", 0)
        length_cm = physical.get("length_cm", 0)

        if width_cm > 0:
            builder.add_class_attribute("width_cm", str(width_cm))
        if length_cm > 0:
            builder.add_class_attribute("length_cm", str(length_cm))

        # Compute rotation center offset from geometric center
        rotation_center = physical.get("rotation_center", {})
        rc_forward, rc_strafe = self._compute_rotation_center_offset(
            width_cm, length_cm, rotation_center
        )
        builder.add_class_attribute("rotation_center_forward_cm", str(rc_forward))
        builder.add_class_attribute("rotation_center_strafe_cm", str(rc_strafe))

        # Build sensor positions dict
        sensors = physical.get("sensors", [])
        definitions = config.get("definitions", {})
        if sensors and width_cm > 0 and length_cm > 0:
            sensor_positions_expr = self._build_sensor_positions_expr(
                sensors, width_cm, length_cm, definitions
            )
            if sensor_positions_expr:
                builder.add_class_attribute("_sensor_positions", sensor_positions_expr)

        # Build wheel positions from kinematics
        drive_config = robot_config.get("drive", {})
        kinematics = drive_config.get("kinematics", {})
        if kinematics:
            wheel_positions_expr = self._build_wheel_positions_expr(kinematics)
            if wheel_positions_expr:
                builder.add_class_attribute("_wheel_positions", wheel_positions_expr)

    def _compute_rotation_center_offset(
        self,
        width_cm: float,
        length_cm: float,
        rotation_center: Dict[str, Any],
    ) -> tuple:
        """
        Compute the rotation center offset from the geometric center.

        Args:
            width_cm: Robot width in cm
            length_cm: Robot length in cm
            rotation_center: Dict with x_cm, y_cm (cm from lower-left origin)

        Returns:
            Tuple of (forward_cm, strafe_cm) offset from geometric center
        """
        if not rotation_center or width_cm <= 0 or length_cm <= 0:
            return (0.0, 0.0)

        # YAML stores x_cm from left, y_cm from back (lower-left origin)
        x_cm = rotation_center.get("x_cm", width_cm / 2)
        y_cm = rotation_center.get("y_cm", length_cm / 2)

        # Convert to offset from geometric center
        # forward_cm: positive = toward front, negative = toward back
        # strafe_cm: positive = toward left, negative = toward right
        forward_cm = y_cm - length_cm / 2
        strafe_cm = (width_cm / 2) - x_cm

        return (round(forward_cm, 4), round(strafe_cm, 4))

    def _build_sensor_positions_expr(
        self,
        sensors: List[Dict[str, Any]],
        width_cm: float,
        length_cm: float,
        definitions: Dict[str, Any],
    ) -> str:
        """
        Build a Python dict expression mapping sensor objects to SensorPosition.

        Args:
            sensors: List of sensor config dicts with name, x_cm, y_cm, clearance_cm
            width_cm: Robot width in cm
            length_cm: Robot length in cm
            definitions: Hardware definitions from config

        Returns:
            Python dict literal string like "{defs.sensor: SensorPosition(...), ...}"
        """
        entries = []

        for sensor_cfg in sensors:
            name = sensor_cfg.get("name")
            # YAML stores x_cm from left, y_cm from back (lower-left origin)
            x_cm = sensor_cfg.get("x_cm")
            y_cm = sensor_cfg.get("y_cm")
            clearance_cm = sensor_cfg.get("clearance_cm", 0)

            # Skip sensors without position data or not in definitions
            if not name or x_cm is None or y_cm is None:
                continue
            if name not in definitions:
                logger.warning(f"Sensor '{name}' not found in definitions, skipping geometry")
                continue

            # Convert to offset from geometric center
            # forward_cm: positive = toward front, negative = toward back
            # strafe_cm: positive = toward left, negative = toward right
            forward_cm = round(y_cm - length_cm / 2, 4)
            strafe_cm = round((width_cm / 2) - x_cm, 4)
            clearance = round(clearance_cm, 4) if clearance_cm else 0

            entries.append(
                f"defs.{name}: SensorPosition(forward_cm={forward_cm}, strafe_cm={strafe_cm}, clearance_cm={clearance})"
            )

        if not entries:
            return ""

        # Format as multi-line dict for readability
        formatted = ",\n        ".join(entries)
        return f"{{\n        {formatted}\n    }}"

    def _build_wheel_positions_expr(self, kinematics: Dict[str, Any]) -> str:
        """
        Build a Python dict expression for wheel positions from kinematics.

        Uses defs.motor_name as keys instead of strings for type safety.

        Args:
            kinematics: Kinematics config with type, track_width, wheelbase, and motor references

        Returns:
            Python dict literal string for wheel positions
        """
        drive_type = kinematics.get("type", "").lower()
        track_width_m = kinematics.get("track_width", 0)
        wheelbase_m = kinematics.get("wheelbase", 0)

        # Convert to cm
        track_width_cm = track_width_m * 100
        wheelbase_cm = wheelbase_m * 100

        if track_width_cm <= 0:
            return ""

        entries = []

        if drive_type == "mecanum":
            # Mecanum: 4 wheels
            if wheelbase_cm <= 0:
                logger.warning("Mecanum kinematics requires wheelbase for wheel positions")
                return ""

            forward = round(wheelbase_cm / 2, 4)
            strafe = round(track_width_cm / 2, 4)

            # Get motor references from kinematics config
            front_left = kinematics.get("front_left_motor")
            front_right = kinematics.get("front_right_motor")
            back_left = kinematics.get("back_left_motor")
            back_right = kinematics.get("back_right_motor")

            if front_left:
                entries.append(f'defs.{front_left}: WheelPosition(forward_cm={forward}, strafe_cm={strafe})')
            if front_right:
                entries.append(f'defs.{front_right}: WheelPosition(forward_cm={forward}, strafe_cm={-strafe})')
            if back_left:
                entries.append(f'defs.{back_left}: WheelPosition(forward_cm={-forward}, strafe_cm={strafe})')
            if back_right:
                entries.append(f'defs.{back_right}: WheelPosition(forward_cm={-forward}, strafe_cm={-strafe})')

        elif drive_type in ("differential", "tank", "two_wheel"):
            # Differential: 2 wheels centered on axle
            strafe = round(track_width_cm / 2, 4)

            # Get motor references from kinematics config
            left = kinematics.get("left_motor")
            right = kinematics.get("right_motor")

            if left:
                entries.append(f'defs.{left}: WheelPosition(forward_cm=0, strafe_cm={strafe})')
            if right:
                entries.append(f'defs.{right}: WheelPosition(forward_cm=0, strafe_cm={-strafe})')
        else:
            # Unknown drive type, skip wheel positions
            logger.info(f"Unknown drive type '{drive_type}', skipping wheel positions")
            return ""

        if not entries:
            return ""

        # Format as multi-line dict
        formatted = ",\n        ".join(entries)
        return f"{{\n        {formatted}\n    }}"

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

        Wraps the new config-based builder with an empty drive config,
        so introspection still discovers required parameters like vel_config and imu.

        Args:
            robot_cfg: Full robot configuration

        Returns:
            Constructor expression string
        """
        # Delegate to the config-based builder with just limits if available
        drive_cfg = {}
        if "limits" in robot_cfg:
            drive_cfg["limits"] = robot_cfg["limits"]
        return self._build_drive_from_config(drive_cfg)

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

            elif param_name == "vel_config":
                # Build ChassisVelocityControlConfig from vel_config configuration
                vel_config_expr = self._build_vel_config(drive_cfg.get("vel_config"))
                drive_args.append(f"vel_config={vel_config_expr}")

            elif param_name == "imu":
                # IMU is always referenced from defs
                drive_args.append("imu=defs.imu")

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

    def _build_vel_config(self, vel_config_cfg: Optional[Dict[str, Any]]) -> str:
        """
        Build ChassisVelocityControlConfig expression.

        If no config is provided, returns the default constructor.
        If config is provided, builds per-axis AxisVelocityControlConfig with
        PidGains and Feedforward nested constructors.

        YAML format:
            vel_config:
              vx:
                pid: {kp: 0.0, ki: 0.0, kd: 0.0}
                ff: {kS: 0.0, kV: 1.0, kA: 0.0}
              vy: ...
              wz: ...

        Args:
            vel_config_cfg: Optional vel_config configuration dict

        Returns:
            Constructor expression string
        """
        # Import the necessary classes
        try:
            chassis_vel_cls = resolve_class("libstp.drive.ChassisVelocityControlConfig")
            self.imports.add(chassis_vel_cls)
        except (ImportError, AttributeError):
            logger.warning("Could not resolve ChassisVelocityControlConfig, using unresolved name")

        if not vel_config_cfg:
            return "ChassisVelocityControlConfig()"

        # Build per-axis configs
        try:
            axis_vel_cls = resolve_class("libstp.drive.AxisVelocityControlConfig")
            self.imports.add(axis_vel_cls)
        except (ImportError, AttributeError):
            pass

        axis_exprs = {}
        for axis_name in ("vx", "vy", "wz"):
            axis_cfg = vel_config_cfg.get(axis_name)
            if axis_cfg:
                axis_exprs[axis_name] = self._build_axis_vel_config(axis_cfg)

        if not axis_exprs:
            return "ChassisVelocityControlConfig()"

        # ChassisVelocityControlConfig only has a default constructor;
        # axes are set via readwrite properties. Generate a helper call.
        # We emit an inline _build_chassis_vel_config(...) helper or
        # assign to a temporary. For clean generated code, use a
        # module-level helper function.
        self._needs_vel_config_helper = True
        args = ", ".join(f"{k}={v}" for k, v in axis_exprs.items())
        return f"_build_chassis_vel_config({args})"

    def _build_axis_vel_config(self, axis_cfg: Dict[str, Any]) -> str:
        """
        Build AxisVelocityControlConfig expression from per-axis config.

        Args:
            axis_cfg: Axis config dict with optional 'pid' and 'ff' keys

        Returns:
            Constructor expression string
        """
        pid_cfg = axis_cfg.get("pid")
        ff_cfg = axis_cfg.get("ff")

        if not pid_cfg and not ff_cfg:
            return "AxisVelocityControlConfig()"

        args = []
        if pid_cfg:
            try:
                pid_cls = resolve_class("libstp.foundation.PidGains")
                self.imports.add(pid_cls)
            except (ImportError, AttributeError):
                pass
            pid_args = ", ".join(
                f"{k}={build_literal_expr(v)}" for k, v in pid_cfg.items()
            )
            args.append(f"pid=PidGains({pid_args})")

        if ff_cfg:
            try:
                ff_cls = resolve_class("libstp.foundation.Feedforward")
                self.imports.add(ff_cls)
            except (ImportError, AttributeError):
                pass
            ff_args = ", ".join(
                f"{k}={build_literal_expr(v)}" for k, v in ff_cfg.items()
            )
            args.append(f"ff=Feedforward({ff_args})")

        return f"AxisVelocityControlConfig({', '.join(args)})"

    @staticmethod
    def _generate_vel_config_helper() -> str:
        """Generate a helper function for building ChassisVelocityControlConfig with custom axes."""
        return (
            "def _build_chassis_vel_config(\n"
            "    vx=None, vy=None, wz=None\n"
            "):\n"
            "    cfg = ChassisVelocityControlConfig()\n"
            "    if vx is not None:\n"
            "        cfg.vx = vx\n"
            "    if vy is not None:\n"
            "        cfg.vy = vy\n"
            "    if wz is not None:\n"
            "        cfg.wz = wz\n"
            "    return cfg\n"
        )

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
                    odometry_class=odometry_class,
                    param_name=name,
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
                f"{key}={self._render_odometry_param_value(params_cfg[key], reference_map, odometry_class=odometry_class, param_name=key)}"
            )

        return f"{odometry_class.__name__}({', '.join(args)})"

    def _render_odometry_param_value(
        self,
        value: Any,
        reference_map: Dict[str, str],
        *,
        odometry_class: Optional[type] = None,
        param_name: Optional[str] = None,
    ) -> str:
        """
        Convert an odometry parameter value to a Python expression.

        When the value is a dict, attempts to introspect the odometry class's
        __init__ signature to determine the expected type and construct a proper
        class instance instead of a raw dict literal.

        Args:
            value: Raw value from configuration
            reference_map: Mapping of known reference names to expressions
            odometry_class: The odometry class (for type introspection on dict values)
            param_name: The parameter name (for type introspection on dict values)

        Returns:
            Python expression string
        """
        if isinstance(value, str):
            if value in reference_map:
                return reference_map[value]
            if value.startswith("defs.") and value[5:] in reference_map:
                return reference_map[value[5:]]
            return build_literal_expr(value)

        if isinstance(value, dict) and odometry_class is not None and param_name is not None:
            # Try to infer the expected class type from the odometry class's signature
            nested_cls = self._infer_odometry_param_class(odometry_class, param_name)
            if nested_cls is not None:
                return build_constructor_expr(
                    nested_cls,
                    value,
                    f"robot.odometry.{param_name}",
                    self.imports,
                )

        return build_literal_expr(value)

    def _infer_odometry_param_class(self, odometry_class: type, param_name: str) -> Optional[type]:
        """
        Infer the expected class type for an odometry __init__ parameter.

        Args:
            odometry_class: The odometry class to introspect
            param_name: The parameter name to look up

        Returns:
            The resolved class type, or None if it cannot be determined
        """
        from ..introspection import infer_param_type

        nested_cls = infer_param_type(odometry_class, param_name)
        if nested_cls is not None:
            logger.info(
                f"Inferred type for odometry param '{param_name}': {nested_cls.__name__}"
            )
            return nested_cls

        logger.warning(
            f"robot.odometry: Could not infer class type for parameter '{param_name}' "
            f"of {odometry_class.__name__}, using dict literal"
        )
        return None

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


    def generate_imports(self) -> str:
        """Generate import statements including Defs, GenericRobot, and mission imports."""
        # Add GenericRobot to imports (will be consolidated with other libstp imports)
        from ..introspection import resolve_class
        try:
            generic_robot_cls = resolve_class("libstp.GenericRobot")
            self.imports.add(generic_robot_cls)
        except (ImportError, AttributeError):
            # Fallback: add import entry directly so GenericRobot always appears
            self.imports._entries.add(("libstp", "GenericRobot"))

        # Add geometry dataclass imports if needed (SensorPosition, WheelPosition)
        if hasattr(self, '_full_config'):
            robot_config = self._full_config.get("robot", {})
            physical = robot_config.get("physical", {})
            drive_config = robot_config.get("drive", {})
            kinematics = drive_config.get("kinematics", {})
            # Import if we have physical sensors or kinematics for wheel positions
            if physical.get("sensors") or kinematics:
                try:
                    sensor_pos_cls = resolve_class("libstp.robot.geometry.SensorPosition")
                    self.imports.add(sensor_pos_cls)
                except (ImportError, AttributeError):
                    self.imports._entries.add(("libstp.robot.geometry", "SensorPosition"))
                try:
                    wheel_pos_cls = resolve_class("libstp.robot.geometry.WheelPosition")
                    self.imports.add(wheel_pos_cls)
                except (ImportError, AttributeError):
                    self.imports._entries.add(("libstp.robot.geometry", "WheelPosition"))

        # Get consolidated libstp imports from ImportSet
        base_imports = super().generate_imports()

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

        parts.append(defs_import)

        if mission_import_lines:
            parts.append("\n" + "\n".join(mission_import_lines))

        return "\n\n".join(parts)
