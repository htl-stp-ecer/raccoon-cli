"""Generator for hardware definitions (defs.py)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseGenerator
from ..builder import build_constructor_expr, build_literal_expr
from ..class_builder import ClassBuilder
from ..introspection import resolve_class
from ..yaml_resolver import create_hardware_resolver

logger = logging.getLogger("raccoon")

# SensorGroup field references (emitted as bare attribute names, not strings).
_SENSOR_GROUP_REF_KEYS = frozenset({"left", "right"})
# SensorGroup optional numeric parameters.
_SENSOR_GROUP_PARAM_KEYS = frozenset({
    "threshold", "speed",
    "follow_speed", "follow_kp", "follow_ki", "follow_kd",
})


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
        self._analog_sensor_class: type | None = None
        self._analog_sensor_fields: list[str] = []

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

            # Validate SensorGroup references
            if hw_cfg.get("type") == "SensorGroup":
                self._validate_sensor_group(field_name, hw_cfg, data)

        # Require button definition
        if "button" not in data:
            raise ValueError(
                "definitions.button is required. Add a DigitalSensor definition:\n"
                "  definitions:\n"
                "    button:\n"
                "      type: DigitalSensor\n"
                "      port: <port_number>"
            )

        button_cfg = data.get("button", {})
        if button_cfg.get("type") != "DigitalSensor":
            raise ValueError(
                f"definitions.button must be of type 'DigitalSensor', got '{button_cfg.get('type')}'"
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
        self._ensure_analog_sensor_class()
        self._analog_sensor_fields = []

        # Build IMU attribute — use config if provided, otherwise bare Imu()
        imu_cfg = data.get("imu", {})
        imu_params = {k: v for k, v in imu_cfg.items() if k != "type"}
        if imu_params:
            imu_expr = self._build_imu_expr(imu_params)
        else:
            imu_expr = "Imu()"

        # Build class attributes
        attributes = [("imu", imu_expr)]
        for field_name, hw_cfg in data.items():
            if field_name == "imu":
                continue

            logger.info(f"Processing definition: {field_name}")

            type_name = hw_cfg.get("type", "")

            # Handle SensorGroup specially — left/right are field references,
            # not constructor params that go through the hardware resolver.
            if type_name == "SensorGroup":
                hw_expr = self._build_sensor_group_expr(hw_cfg)
                attributes.append((field_name, hw_expr))
                logger.info(f"Generated SensorGroup '{field_name}'")
                continue

            # Check for servo preset (positions key on a Servo type)
            preset_info = self._extract_servo_preset(hw_cfg)

            # Resolve type and extract parameters using the unified resolver
            # Strip preset-only keys before passing to the hardware resolver
            resolved_cfg = {k: v for k, v in hw_cfg.items() if k not in ("positions", "offset")}
            try:
                hw_class, hw_params = self.resolver.resolve_from_config(resolved_cfg, type_key="type")
                logger.info(f"Resolved type '{type_name}' to {hw_class.__name__} for {field_name}")
            except ValueError as e:
                raise ValueError(f"definitions.{field_name}: {e}")

            # Track analog sensors
            if self._is_analog_sensor(hw_class):
                self._analog_sensor_fields.append(field_name)
                logger.info(f"Field '{field_name}' is an analog sensor")

            # Build constructor expression (with type checking)
            hw_expr = build_constructor_expr(
                hw_class, hw_params, f"definitions.{field_name}", self.imports
            )

            # Wrap in ServoPreset if positions were defined
            if preset_info is not None:
                positions, offset = preset_info
                hw_expr = self._build_servo_preset_expr(hw_expr, positions, offset)
                logger.info(f"Wrapping '{field_name}' as ServoPreset with {len(positions)} positions (offset={offset})")

            attributes.append((field_name, hw_expr))

        # Always add analog_sensors list (empty if no analog sensors found)
        analog_list = "[" + ", ".join(self._analog_sensor_fields) + "]"
        attributes.append(("analog_sensors", analog_list))

        # Use ClassBuilder to construct the class
        return ClassBuilder.build_simple_class(self.class_name, attributes)

    @staticmethod
    def _extract_servo_preset(hw_cfg: Dict[str, Any]) -> Optional[Tuple[Dict[str, float], float]]:
        """
        Check if a hardware definition includes servo preset positions.

        Returns:
            (positions_dict, offset) if positions are defined, None otherwise.
        """
        if hw_cfg.get("type") != "Servo":
            return None
        positions = hw_cfg.get("positions")
        if not positions:
            return None
        if not isinstance(positions, dict):
            return None
        offset = float(hw_cfg.get("offset", 0))
        return positions, offset

    def _build_servo_preset_expr(
        self,
        servo_expr: str,
        positions: Dict[str, float],
        offset: float,
    ) -> str:
        """Build a ServoPreset(...) expression wrapping a Servo constructor."""
        self.imports._entries.add(("libstp.step.servo.preset", "ServoPreset"))
        positions_literal = build_literal_expr(positions)
        if offset:
            return f"ServoPreset({servo_expr}, positions={positions_literal}, offset={build_literal_expr(offset)})"
        return f"ServoPreset({servo_expr}, positions={positions_literal})"

    def _build_imu_expr(self, params: Dict[str, Any]) -> str:
        """Build IMU constructor expression from config params."""
        pieces = []
        for name, value in params.items():
            pieces.append(f"{name}={build_literal_expr(value)}")
        return "Imu(" + ", ".join(pieces) + ")"

    def _ensure_analog_sensor_class(self) -> None:
        """Resolve the AnalogSensor class for isinstance checking."""
        if self._analog_sensor_class is not None:
            return

        candidates = [
            "libstp.AnalogSensor",
            "libstp.hal.AnalogSensor",
            "libstp.foundation.AnalogSensor",
        ]

        for qualname in candidates:
            try:
                self._analog_sensor_class = resolve_class(qualname)
                logger.debug(f"Resolved AnalogSensor from '{qualname}'")
                return
            except (ImportError, AttributeError):
                continue

        logger.warning(
            "Could not resolve AnalogSensor class. "
            "analog_sensors list will not be generated."
        )

    def _is_analog_sensor(self, hw_class: type) -> bool:
        """Check if a hardware class is a subclass of AnalogSensor."""
        if self._analog_sensor_class is None:
            return False
        try:
            return issubclass(hw_class, self._analog_sensor_class)
        except TypeError:
            return False

    def _ensure_imu_import(self) -> None:
        """Resolve the appropriate import line for the Imu definition."""
        if self._imu_import_line is not None:
            return

        candidates = [
            ("from libstp import Imu", "libstp.imu.Imu"),
            ("from libstp import IMU as Imu", "libstp.hal.IMU"),
        ]

        for import_line, qualname in candidates:
            try:
                resolve_class(qualname)
                self._imu_import_line = import_line
                logger.debug(f"Using '{import_line}' for Imu definition")
                return
            except (ImportError, AttributeError):
                continue

        # Fall back to libstp import so generation still succeeds.
        self._imu_import_line = "from libstp import IMU as Imu"
        logger.warning(
            "Could not import libstp.imu.Imu or libstp.hal.IMU during generation. "
            "Defaulting to 'from libstp import IMU as Imu'; ensure the target "
            "environment provides a compatible IMU class."
        )

    @staticmethod
    def _validate_sensor_group(
        field_name: str, hw_cfg: Dict[str, Any], data: Dict[str, Any]
    ) -> None:
        """Validate a SensorGroup definition entry."""
        # At least one of left/right must be specified
        if "left" not in hw_cfg and "right" not in hw_cfg:
            raise ValueError(
                f"definitions.{field_name}: "
                "SensorGroup must specify at least 'left' or 'right'"
            )
        # Validate sensor references point to existing definitions
        hw_fields = {k for k in data if k != field_name}
        for side in ("left", "right"):
            ref = hw_cfg.get(side)
            if ref is not None and ref not in hw_fields:
                raise ValueError(
                    f"definitions.{field_name}.{side}: "
                    f"'{ref}' does not match any definition. "
                    f"Available: {', '.join(sorted(hw_fields))}"
                )

    def _build_sensor_group_expr(self, hw_cfg: Dict[str, Any]) -> str:
        """Build a SensorGroup(...) constructor expression."""
        self.imports._entries.add(
            ("libstp.step.motion.sensor_group", "SensorGroup")
        )

        pieces: List[str] = []
        # left/right are bare field references (class attribute names)
        for key in ("left", "right"):
            ref = hw_cfg.get(key)
            if ref is not None:
                pieces.append(f"{key}={ref}")
        # Optional numeric parameters
        for key in sorted(_SENSOR_GROUP_PARAM_KEYS):
            val = hw_cfg.get(key)
            if val is not None:
                pieces.append(f"{key}={build_literal_expr(val)}")

        return "SensorGroup(" + ", ".join(pieces) + ")"

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
