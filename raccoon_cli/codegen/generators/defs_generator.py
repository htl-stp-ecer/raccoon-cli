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
_SENSOR_GROUP_PARAM_KEYS = frozenset(
    {
        "threshold",
        "speed",
        "follow_speed",
        "follow_kp",
        "follow_ki",
        "follow_kd",
    }
)
# Extra keys on wait_for_light_sensor that are stripped from the hardware
# constructor and emitted as separate Defs class attributes instead.
_WFL_EXTRA_KEYS = frozenset({"mode", "drop_fraction"})


class DefsGenerator(BaseGenerator):
    """Generator for hardware definitions file (defs.py)."""

    def __init__(self, class_name: str = "Defs"):
        super().__init__(class_name)
        self.resolver = create_hardware_resolver()
        self._imu_class = resolve_class("raccoon.IMU")
        self._analog_sensor_class = resolve_class("raccoon.AnalogSensor")
        self._analog_sensor_fields: list[str] = []

    def get_output_filename(self) -> str:
        return "defs.py"

    def extract_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        definitions = config.get("definitions")
        if definitions is None:
            logger.warning("No 'definitions' key found in config")
            return {}
        if not isinstance(definitions, dict):
            return definitions

        # Backward compatibility: older projects may keep grouped hardware
        # under helper keys such as "_motors" using !include (not !include-merge).
        # Flatten those sections so downstream validation/codegen sees normal
        # top-level hardware entries with "type" fields.
        flattened = dict(definitions)
        grouped_keys = [
            key
            for key, value in definitions.items()
            if isinstance(key, str) and key.startswith("_") and isinstance(value, dict)
        ]
        for group_key in grouped_keys:
            group_data = definitions[group_key]
            del flattened[group_key]
            for name, hw_cfg in group_data.items():
                if name in flattened:
                    raise ValueError(
                        f"definitions.{group_key}: duplicate definition '{name}' "
                        "already exists at top level"
                    )
                flattened[name] = hw_cfg
        return flattened

    def validate_config(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError(
                "Top-level config must contain a mapping under key 'definitions:'"
            )

        for field_name, hw_cfg in data.items():
            if not isinstance(hw_cfg, dict):
                raise ValueError(f"definitions.{field_name} must be a mapping")

            if not field_name.isidentifier():
                raise ValueError(
                    f"definitions.{field_name}: not a valid Python identifier"
                )

            if "type" not in hw_cfg:
                raise ValueError(
                    f"definitions.{field_name}: missing required 'type' field"
                )

            if hw_cfg.get("type") == "SensorGroup":
                self._validate_sensor_group(field_name, hw_cfg, data)

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
        self._analog_sensor_fields = []

        imu_cfg = data.get("imu", {})
        imu_params = {k: v for k, v in imu_cfg.items() if k != "type"}
        if imu_params:
            imu_expr = self._build_imu_expr(imu_params)
        else:
            imu_expr = "IMU()"
        self.imports.add(self._imu_class)

        attributes = [("imu", imu_expr)]
        wfl_extra_attrs: List[Tuple[str, str]] = []

        for field_name, hw_cfg in data.items():
            if field_name == "imu":
                continue

            logger.info(f"Processing definition: {field_name}")

            type_name = hw_cfg.get("type", "")

            if type_name == "SensorGroup":
                hw_expr = self._build_sensor_group_expr(hw_cfg)
                attributes.append((field_name, hw_expr))
                logger.info(f"Generated SensorGroup '{field_name}'")
                continue

            preset_info = self._extract_servo_preset(hw_cfg)

            strip_keys = {"positions", "offset"}
            is_wfl_sensor = field_name == "wait_for_light_sensor"
            if is_wfl_sensor:
                strip_keys |= _WFL_EXTRA_KEYS

            resolved_cfg = {k: v for k, v in hw_cfg.items() if k not in strip_keys}
            try:
                hw_class, hw_params = self.resolver.resolve_from_config(
                    resolved_cfg, type_key="type"
                )
                logger.info(
                    f"Resolved type '{type_name}' to {hw_class.__name__} for {field_name}"
                )
            except ValueError as e:
                # Compatibility fallback: type index can lag behind runtime classes
                # during client/server version skew. Emit a constructor directly so
                # codegen remains usable for standard hardware definitions.
                if isinstance(type_name, str) and type_name:
                    logger.info(
                        "Falling back to unresolved constructor for "
                        f"definitions.{field_name} ({type_name}): {e}"
                    )
                    hw_params = {k: v for k, v in resolved_cfg.items() if k != "type"}
                    self.imports._entries.add(("raccoon.hal", type_name))
                    fallback_params = dict(hw_params)
                    if type_name == "Motor":
                        calibration = fallback_params.get("calibration")
                        if isinstance(calibration, dict):
                            self.imports._entries.add(
                                ("raccoon.foundation", "MotorCalibration")
                            )
                            cal_args = ", ".join(
                                f"{k}={build_literal_expr(v)}"
                                for k, v in calibration.items()
                            )
                            fallback_params["calibration"] = (
                                f"MotorCalibration({cal_args})"
                            )

                    args_parts: list[str] = []
                    for k, v in fallback_params.items():
                        if isinstance(v, str) and v.startswith("MotorCalibration("):
                            args_parts.append(f"{k}={v}")
                        else:
                            args_parts.append(f"{k}={build_literal_expr(v)}")
                    args = ", ".join(args_parts)
                    hw_expr = f"{type_name}({args})" if args else f"{type_name}()"

                    # Preserve ServoPreset wrapping even when we had to fall back
                    # to an unresolved constructor path.
                    if preset_info is not None:
                        positions, offset = preset_info
                        hw_expr = self._build_servo_preset_expr(
                            hw_expr, positions, offset
                        )

                    attributes.append((field_name, hw_expr))
                    continue
                raise ValueError(f"definitions.{field_name}: {e}")

            if self._is_analog_sensor(hw_class):
                self._analog_sensor_fields.append(field_name)
                logger.info(f"Field '{field_name}' is an analog sensor")

            hw_expr = build_constructor_expr(
                hw_class, hw_params, f"definitions.{field_name}", self.imports
            )

            if preset_info is not None:
                positions, offset = preset_info
                hw_expr = self._build_servo_preset_expr(hw_expr, positions, offset)
                logger.info(
                    f"Wrapping '{field_name}' as ServoPreset with {len(positions)} positions (offset={offset})"
                )

            attributes.append((field_name, hw_expr))

            if is_wfl_sensor:
                wfl_mode = hw_cfg.get("mode", "auto")
                wfl_drop = hw_cfg.get("drop_fraction")
                wfl_extra_attrs.append(("wait_for_light_mode", f'"{wfl_mode}"'))
                if wfl_drop is not None:
                    wfl_extra_attrs.append(
                        (
                            "wait_for_light_drop_fraction",
                            build_literal_expr(float(wfl_drop)),
                        )
                    )
                logger.info(
                    f"WFL config: mode={wfl_mode}"
                    + (f", drop_fraction={wfl_drop}" if wfl_drop is not None else "")
                )

        analog_list = "[" + ", ".join(self._analog_sensor_fields) + "]"
        attributes.append(("analog_sensors", analog_list))
        attributes.extend(wfl_extra_attrs)

        return ClassBuilder.build_simple_class(self.class_name, attributes)

    @staticmethod
    def _extract_servo_preset(
        hw_cfg: Dict[str, Any],
    ) -> Optional[Tuple[Dict[str, float], float]]:
        """
        Check if a hardware definition includes servo preset positions.

        Returns:
            (positions_dict, offset) if positions are defined, None otherwise.
        """
        if hw_cfg.get("type") != "Servo":
            return None
        positions = hw_cfg.get("positions")
        if not positions or not isinstance(positions, dict):
            return None
        offset = float(hw_cfg.get("offset", 0))
        return positions, offset

    def _build_servo_preset_expr(
        self,
        servo_expr: str,
        positions: Dict[str, float],
        offset: float,
    ) -> str:
        preset_cls = resolve_class("raccoon.ServoPreset")
        self.imports.add(preset_cls)
        positions_literal = build_literal_expr(positions)
        if offset:
            return f"ServoPreset({servo_expr}, positions={positions_literal}, offset={build_literal_expr(offset)})"
        return f"ServoPreset({servo_expr}, positions={positions_literal})"

    def _build_imu_expr(self, params: Dict[str, Any]) -> str:
        pieces = [
            f"{name}={build_literal_expr(value)}" for name, value in params.items()
        ]
        return "IMU(" + ", ".join(pieces) + ")"

    def _is_analog_sensor(self, hw_class: type) -> bool:
        try:
            return issubclass(hw_class, self._analog_sensor_class)
        except TypeError:
            return False

    @staticmethod
    def _validate_sensor_group(
        field_name: str, hw_cfg: Dict[str, Any], data: Dict[str, Any]
    ) -> None:
        if "left" not in hw_cfg and "right" not in hw_cfg:
            raise ValueError(
                f"definitions.{field_name}: "
                "SensorGroup must specify at least 'left' or 'right'"
            )
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
        sg_cls = resolve_class("raccoon.SensorGroup")
        self.imports.add(sg_cls)

        pieces: List[str] = []
        for key in ("left", "right"):
            ref = hw_cfg.get(key)
            if ref is not None:
                pieces.append(f"{key}={ref}")
        for key in sorted(_SENSOR_GROUP_PARAM_KEYS):
            val = hw_cfg.get(key)
            if val is not None:
                pieces.append(f"{key}={build_literal_expr(val)}")

        return "SensorGroup(" + ", ".join(pieces) + ")"

    def generate_footer(self) -> str:
        instance_name = self.class_name[0].lower() + self.class_name[1:]
        return (
            f"\n{instance_name} = {self.class_name}()\n"
            f"\n__all__ = ['{self.class_name}', '{instance_name}']\n"
        )

    def generate_imports(self) -> str:
        return super().generate_imports()
