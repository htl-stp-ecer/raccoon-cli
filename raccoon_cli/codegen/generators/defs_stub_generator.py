"""Generator for defs.pyi type stub — gives IDEs full autocomplete for ServoPresets."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from .base import BaseGenerator
from ..yaml_resolver import create_hardware_resolver

logger = logging.getLogger("raccoon")

# Maps YAML type names to their qualified import paths for the stub
_TYPE_IMPORTS = {
    "Motor": "Motor",
    "Servo": "Servo",
    "IRSensor": "IRSensor",
    "ETSensor": "ETSensor",
    "AnalogSensor": "AnalogSensor",
    "DigitalSensor": "DigitalSensor",
}


class DefsStubGenerator(BaseGenerator):
    """
    Generator for defs.pyi type stub file.

    Produces type annotations so IDEs can autocomplete ServoPreset
    position methods (e.g. Defs.pom_arm.down()) and all other
    hardware attributes.
    """

    def __init__(self, class_name: str = "Defs"):
        super().__init__(class_name)
        self.resolver = create_hardware_resolver()

    def get_output_filename(self) -> str:
        return "defs.pyi"

    def extract_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        definitions = config.get("definitions")
        if definitions is None:
            logger.warning("No 'definitions' key found in config")
            return {}
        return definitions

    def validate_config(self, data: Dict[str, Any]) -> None:
        # defs.py generator handles validation; stub just needs definitions
        if not isinstance(data, dict):
            raise ValueError("definitions must be a mapping")

    def generate_header(self, config: Dict[str, Any]) -> str:
        # Minimal header for stub files
        return '"""Auto-generated type stub — Raccoon Toolchain (Tobias Madlberger / RaccoonOS Team)"""'

    def generate_footer(self) -> str:
        return ""

    def generate_imports(self) -> str:
        return ""  # We build imports inline in generate_body

    def generate_body(self, data: Dict[str, Any]) -> str:
        lines: List[str] = []
        imports: set[str] = set()
        preset_classes: List[str] = []

        # Always need these
        imports.add("from typing import List")
        imports.add("from raccoon.step.servo.preset import ServoPreset, _PresetPosition")

        # Collect field info
        fields: List[Tuple[str, str]] = []  # (name, type_str)

        # IMU is always first
        imports.add("from raccoon import IMU as Imu")
        fields.append(("imu", "Imu"))

        has_wfl_sensor = False
        wfl_has_drop_fraction = False

        for field_name, hw_cfg in data.items():
            if field_name == "imu":
                continue

            type_name = hw_cfg.get("type", "")
            positions = hw_cfg.get("positions")

            if type_name == "SensorGroup":
                # SensorGroup is a regular definition type
                imports.add("from raccoon.step.motion.sensor_group import SensorGroup")
                fields.append((field_name, "SensorGroup"))
            elif type_name == "Servo" and positions and isinstance(positions, dict):
                # Generate a typed preset class for this servo
                class_name = f"_{_to_camel(field_name)}Preset"
                preset_classes.append(
                    _build_preset_class(class_name, positions)
                )
                fields.append((field_name, class_name))
            else:
                # Regular hardware type
                import_name = _TYPE_IMPORTS.get(type_name, type_name)
                if import_name:
                    imports.add(f"from raccoon import {import_name}")
                fields.append((field_name, import_name or "Any"))

            if field_name == "wait_for_light_sensor":
                has_wfl_sensor = True
                wfl_has_drop_fraction = "drop_fraction" in hw_cfg

        # analog_sensors list
        imports.add("from raccoon import AnalogSensor")
        fields.append(("analog_sensors", "List[AnalogSensor]"))

        # wait_for_light config attributes (generated alongside wait_for_light_sensor)
        if has_wfl_sensor:
            fields.append(("wait_for_light_mode", "str"))
            if wfl_has_drop_fraction:
                fields.append(("wait_for_light_drop_fraction", "float"))

        # Assemble output
        lines.extend(sorted(imports))
        lines.append("")

        # Preset protocol classes
        for cls_code in preset_classes:
            lines.append("")
            lines.append(cls_code)

        # Defs class
        lines.append("")
        lines.append(f"class {self.class_name}:")
        for name, type_str in fields:
            lines.append(f"    {name}: {type_str}")

        return "\n".join(lines)


def _to_camel(snake: str) -> str:
    """Convert snake_case to CamelCase."""
    return "".join(part.capitalize() for part in snake.split("_"))


def _build_preset_class(class_name: str, positions: Dict[str, float]) -> str:
    """Build a typed ServoPreset subclass stub with position methods."""
    lines = [f"class {class_name}(ServoPreset):"]
    for pos_name in positions:
        lines.append(f"    {pos_name}: _PresetPosition")
    # Also expose .device property
    lines.append("    @property")
    lines.append("    def device(self) -> 'Servo': ...")
    return "\n".join(lines)
