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
    """Generator for robot configuration file (robot.py)."""

    def __init__(self, class_name: str = "Robot"):
        super().__init__(class_name)
        self.kinematics_resolver = create_kinematics_resolver()
        self.odometry_resolver = create_odometry_resolver()
        self.mission_imports = []

    def get_output_filename(self) -> str:
        return "robot.py"

    def extract_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        robot_config = config.get("robot")
        if robot_config is None:
            logger.warning("No 'robot' key found in config")
            return {}

        result = dict(robot_config)

        missions = config.get("missions", [])
        if missions:
            result["_missions"] = missions
        definitions = config.get("definitions", {})
        if definitions:
            result["_definitions"] = definitions

        return result

    def validate_config(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("Robot config must be a mapping under key 'robot:'")

        if "motion_pid" not in data:
            raise ValueError(
                "robot.motion_pid is required. Please add a 'motion_pid' block under 'robot'."
            )
        if not isinstance(data["motion_pid"], dict):
            raise ValueError("robot.motion_pid must be a mapping")

        if "drive" in data:
            drive = data["drive"]
            if not isinstance(drive, dict):
                raise ValueError("robot.drive must be a mapping")

            if "kinematics" in drive:
                kinematics = drive["kinematics"]
                if not isinstance(kinematics, dict):
                    raise ValueError("robot.drive.kinematics must be a mapping")
                if "type" not in kinematics:
                    raise ValueError("robot.drive.kinematics.type is required")

        elif "kinematics" in data:
            kinematics = data["kinematics"]
            if not isinstance(kinematics, dict):
                raise ValueError("robot.kinematics must be a mapping")
            if "type" not in kinematics:
                raise ValueError("robot.kinematics.type is required")

        if "odometry" in data:
            odometry_cfg = data["odometry"]
            if not isinstance(odometry_cfg, (str, dict)):
                raise ValueError("robot.odometry must be a string or a mapping")
            if isinstance(odometry_cfg, dict) and "type" not in odometry_cfg:
                raise ValueError("robot.odometry.type is required when using mapping form")

    def generate(self, config: Dict[str, Any]) -> str:
        self._full_config = config
        return super().generate(config)

    def write(self, config: Dict[str, Any], output_dir: Path, format_code: bool = True) -> Path:
        self._full_config = config
        return super().write(config, output_dir, format_code)

    def generate_body(self, data: Dict[str, Any]) -> str:
        if not data:
            return f"class {self.class_name}:\n    pass"

        self._needs_vel_config_helper = False
        builder = ClassBuilder(self.class_name, base_classes=["GenericRobot"])

        builder.add_class_attribute("defs", "Defs()")

        kinematics_expr = ""
        drive_expr = ""

        if "drive" in data:
            drive_cfg = data["drive"]
            if "kinematics" in drive_cfg:
                kinematics_expr = self._build_kinematics(drive_cfg["kinematics"])
            drive_expr = self._build_drive_from_config(drive_cfg)

        elif "kinematics" in data:
            kinematics_expr = self._build_kinematics(data["kinematics"])
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

        motion_pid_cfg = data.get("motion_pid")
        if motion_pid_cfg is not None:
            motion_pid_class = resolve_class("raccoon.UnifiedMotionPidConfig")
            self.imports.add(motion_pid_class)
            motion_pid_expr = build_constructor_expr(
                motion_pid_class, motion_pid_cfg, "robot.motion_pid", self.imports
            )
            builder.add_class_attribute("motion_pid_config", motion_pid_expr)

        shutdown_in = data.get("shutdown_in")
        if shutdown_in is None:
            raise ValueError(
                "Missing required 'shutdown_in' in raccoon.project.yml"
            )
        builder.add_class_attribute("shutdown_in", repr(shutdown_in))

        if hasattr(self, '_full_config'):
            self._add_missions_to_builder(builder, self._full_config)

        if hasattr(self, '_full_config'):
            self._add_geometry_to_builder(builder, self._full_config)

        parts = []

        if self._needs_vel_config_helper:
            parts.append(self._generate_vel_config_helper())
            parts.append("")

        parts.append(builder.build())
        return "\n".join(parts)

    def _add_missions_to_builder(self, builder: ClassBuilder, config: Dict[str, Any]) -> None:
        missions = config.get("missions", [])
        if not missions:
            return

        normal_missions = []
        setup_mission = None
        shutdown_mission = None

        for mission_entry in missions:
            if isinstance(mission_entry, str):
                mission_name = mission_entry
                mission_type = "normal"
            elif isinstance(mission_entry, dict):
                mission_name = list(mission_entry.keys())[0]
                mission_type = mission_entry[mission_name]
            else:
                logger.warning(f"Skipping invalid mission entry: {mission_entry}")
                continue

            mission_file = self._class_name_to_snake_case(mission_name)

            self.mission_imports.append({
                "class_name": mission_name,
                "file_name": mission_file,
                "type": mission_type
            })

            if mission_type == "setup":
                setup_mission = mission_name
            elif mission_type == "shutdown":
                shutdown_mission = mission_name
            else:
                normal_missions.append(mission_name)

        if normal_missions:
            mission_instances = ", ".join([f"{name}()" for name in normal_missions])
            formatted_instances = mission_instances.replace(", ", ",\n        ")
            builder.add_class_attribute(
                "missions",
                f"[\n        {formatted_instances}\n    ]",
            )

        builder.add_class_attribute("setup_mission", f"{setup_mission}()" if setup_mission else "None")
        builder.add_class_attribute("shutdown_mission", f"{shutdown_mission}()" if shutdown_mission else "None")

    def _add_geometry_to_builder(self, builder: ClassBuilder, config: Dict[str, Any]) -> None:
        robot_config = config.get("robot", {})
        physical = robot_config.get("physical", {})

        if not physical:
            return

        width_cm = physical.get("width_cm", 0)
        length_cm = physical.get("length_cm", 0)

        if width_cm > 0:
            builder.add_class_attribute("width_cm", str(width_cm))
        if length_cm > 0:
            builder.add_class_attribute("length_cm", str(length_cm))

        rotation_center = physical.get("rotation_center", {})
        rc_forward, rc_strafe = self._compute_rotation_center_offset(
            width_cm, length_cm, rotation_center
        )
        builder.add_class_attribute("rotation_center_forward_cm", str(rc_forward))
        builder.add_class_attribute("rotation_center_strafe_cm", str(rc_strafe))

        sensors = physical.get("sensors", [])
        definitions = config.get("definitions", {})
        if sensors and width_cm > 0 and length_cm > 0:
            sensor_positions_expr = self._build_sensor_positions_expr(
                sensors, width_cm, length_cm, definitions
            )
            if sensor_positions_expr:
                builder.add_class_attribute("_sensor_positions", sensor_positions_expr)

        drive_config = robot_config.get("drive", {})
        kinematics = drive_config.get("kinematics", {})
        if kinematics:
            wheel_positions_expr = self._build_wheel_positions_expr(kinematics)
            if wheel_positions_expr:
                builder.add_class_attribute("_wheel_positions", wheel_positions_expr)

        table_map_data = physical.get("table_map")
        if table_map_data:
            if isinstance(table_map_data, str):
                raise ValueError(
                    f"table_map '{table_map_data}' was not resolved to a dict — "
                    "ensure the .ftmap path is relative to the repository root"
                )
            table_map_expr = self._build_table_map_expr(table_map_data)
            builder.add_class_attribute("table_map", table_map_expr)

    def _compute_rotation_center_offset(
        self,
        width_cm: float,
        length_cm: float,
        rotation_center: Dict[str, Any],
    ) -> tuple:
        if not rotation_center or width_cm <= 0 or length_cm <= 0:
            return (0.0, 0.0)

        x_cm = rotation_center.get("x_cm", width_cm / 2)
        y_cm = rotation_center.get("y_cm", length_cm / 2)

        forward_cm = y_cm - length_cm / 2
        strafe_cm = (width_cm / 2) - x_cm

        return (round(forward_cm, 4), round(strafe_cm, 4))

    def _build_table_map_expr(self, data: Dict[str, Any]) -> str:
        table_map_cls = resolve_class("raccoon.TableMap")
        self.imports.add(table_map_cls)
        return f"TableMap.from_ftmap({data!r})"

    def _build_sensor_positions_expr(
        self,
        sensors: List[Dict[str, Any]],
        width_cm: float,
        length_cm: float,
        definitions: Dict[str, Any],
    ) -> str:
        entries = []

        for sensor_cfg in sensors:
            name = sensor_cfg.get("name")
            x_cm = sensor_cfg.get("x_cm")
            y_cm = sensor_cfg.get("y_cm")
            clearance_cm = sensor_cfg.get("clearance_cm", 0)

            if not name or x_cm is None or y_cm is None:
                continue
            if name not in definitions:
                logger.warning(f"Sensor '{name}' not found in definitions, skipping geometry")
                continue

            forward_cm = round(y_cm - length_cm / 2, 4)
            strafe_cm = round((width_cm / 2) - x_cm, 4)
            clearance = round(clearance_cm, 4) if clearance_cm else 0

            entries.append(
                f"defs.{name}: SensorPosition(forward_cm={forward_cm}, strafe_cm={strafe_cm}, clearance_cm={clearance})"
            )

        if not entries:
            return ""

        formatted = ",\n        ".join(entries)
        return f"{{\n        {formatted}\n    }}"

    def _build_wheel_positions_expr(self, kinematics: Dict[str, Any]) -> str:
        drive_type = kinematics.get("type", "").lower()
        track_width_m = kinematics.get("track_width", 0)
        wheelbase_m = kinematics.get("wheelbase", 0)

        track_width_cm = track_width_m * 100
        wheelbase_cm = wheelbase_m * 100

        if track_width_cm <= 0:
            return ""

        entries = []

        if drive_type == "mecanum":
            if wheelbase_cm <= 0:
                logger.warning("Mecanum kinematics requires wheelbase for wheel positions")
                return ""

            forward = round(wheelbase_cm / 2, 4)
            strafe = round(track_width_cm / 2, 4)

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
            strafe = round(track_width_cm / 2, 4)

            left = kinematics.get("left_motor")
            right = kinematics.get("right_motor")

            if left:
                entries.append(f'defs.{left}: WheelPosition(forward_cm=0, strafe_cm={strafe})')
            if right:
                entries.append(f'defs.{right}: WheelPosition(forward_cm=0, strafe_cm={-strafe})')
        else:
            logger.info(f"Unknown drive type '{drive_type}', skipping wheel positions")
            return ""

        if not entries:
            return ""

        formatted = ",\n        ".join(entries)
        return f"{{\n        {formatted}\n    }}"

    @staticmethod
    def _class_name_to_snake_case(class_name: str) -> str:
        import re
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', class_name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    HARDWARE_REF_PARAMS = {
        "left_motor", "right_motor",
        "front_left_motor", "front_right_motor", "back_left_motor", "back_right_motor",
        "rear_left_motor", "rear_right_motor",
    }

    def _validate_hardware_refs(self, hardware_refs: Dict[str, str]) -> None:
        if not hardware_refs:
            return

        if not hasattr(self, '_full_config'):
            logger.warning("Cannot validate hardware references: full config not available")
            return

        definitions = self._full_config.get("definitions", {})
        if isinstance(definitions, dict):
            # Backward compatibility: flatten grouped definitions loaded via
            # keys like "_motors", "_sensors", "_servos".
            flattened = dict(definitions)
            grouped_keys = [
                key for key, value in definitions.items()
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
            definitions = flattened

        if not definitions:
            raise ValueError(
                "No hardware definitions found in config, but kinematics references hardware: "
                f"{', '.join(hardware_refs.values())}"
            )

        missing_refs = [
            f"{param_name}='{def_name}'"
            for param_name, def_name in hardware_refs.items()
            if def_name not in definitions
        ]

        if missing_refs:
            available_defs = ', '.join(sorted(definitions.keys()))
            raise ValueError(
                f"robot.drive.kinematics: Hardware reference(s) not found in definitions: "
                f"{', '.join(missing_refs)}. "
                f"Available definitions: {available_defs}"
            )

    def _build_kinematics(self, kinematics_cfg: Dict[str, Any]) -> str:
        kinematics_type = kinematics_cfg.get("type", "")
        if not kinematics_type:
            logger.error("Kinematics type is required")
            return ""

        kinematics_class = self.kinematics_resolver.resolve_type(kinematics_type)
        logger.info(f"Resolved kinematics type '{kinematics_type}' to {kinematics_class.__name__}")

        params = {}
        for key, value in kinematics_cfg.items():
            if key == "type":
                continue
            if isinstance(value, str) and key in self.HARDWARE_REF_PARAMS:
                params[key] = ("__hardware_ref__", value)
            else:
                params[key] = value

        hardware_refs = {
            key: value[1]
            for key, value in params.items()
            if isinstance(value, tuple) and value[0] == "__hardware_ref__"
        }

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

        unknown_params = provided_params - set(init_params.keys())
        if unknown_params:
            logger.warning(
                f"robot.kinematics: Unknown parameter(s) for {kinematics_class.__name__}: "
                f"{', '.join(sorted(unknown_params))}. "
                f"Valid parameters: {', '.join(sorted(init_params.keys()))}"
            )

        self._validate_hardware_refs(hardware_refs)
        self.imports.add(kinematics_class)

        args = []
        for key in sorted(params.keys()):
            value = params[key]
            if isinstance(value, tuple) and value[0] == "__hardware_ref__":
                args.append(f"{key}=defs.{value[1]}")
            else:
                args.append(f"{key}={build_literal_expr(value)}")

        return f"{kinematics_class.__name__}({', '.join(args)})"

    def _build_drive_legacy(self, robot_cfg: Dict[str, Any]) -> str:
        drive_cfg = {}
        if "limits" in robot_cfg:
            drive_cfg["limits"] = robot_cfg["limits"]
        return self._build_drive_from_config(drive_cfg)

    def _build_drive_from_config(self, drive_cfg: Dict[str, Any]) -> str:
        drive_class = resolve_class("raccoon.Drive")
        self.imports.add(drive_class)

        from ..introspection import get_init_params
        import inspect

        init_params = get_init_params(drive_class)
        logger.info(f"Drive.__init__ parameters: {list(init_params.keys())}")

        drive_args = []

        for param_name, param in init_params.items():
            if param_name == "kinematics":
                drive_args.append("kinematics=kinematics")

            elif param_name == "vel_config":
                vel_config_expr = self._build_vel_config(drive_cfg.get("vel_config"))
                drive_args.append(f"vel_config={vel_config_expr}")

            elif param_name == "imu":
                drive_args.append("imu=defs.imu")

            else:
                if param_name in drive_cfg:
                    value = drive_cfg[param_name]
                    drive_args.append(f"{param_name}={build_literal_expr(value)}")
                elif param.default == inspect.Parameter.empty:
                    logger.error(f"Missing required parameter '{param_name}' for Drive constructor")
                    return ""

        return f"Drive({', '.join(drive_args)})"

    def _build_vel_config(self, vel_config_cfg: Optional[Dict[str, Any]]) -> str:
        chassis_vel_cls = resolve_class("raccoon.ChassisVelocityControlConfig")
        self.imports.add(chassis_vel_cls)

        if not vel_config_cfg:
            return "ChassisVelocityControlConfig()"

        axis_vel_cls = resolve_class("raccoon.AxisVelocityControlConfig")
        self.imports.add(axis_vel_cls)

        axis_exprs = {
            axis_name: self._build_axis_vel_config(axis_cfg)
            for axis_name in ("vx", "vy", "wz")
            if (axis_cfg := vel_config_cfg.get(axis_name))
        }

        if not axis_exprs:
            return "ChassisVelocityControlConfig()"

        self._needs_vel_config_helper = True
        args = ", ".join(f"{k}={v}" for k, v in axis_exprs.items())
        return f"_build_chassis_vel_config({args})"

    def _build_axis_vel_config(self, axis_cfg: Dict[str, Any]) -> str:
        pid_cfg = axis_cfg.get("pid")
        ff_cfg = axis_cfg.get("ff")

        if not pid_cfg and not ff_cfg:
            return "AxisVelocityControlConfig()"

        args = []
        if pid_cfg:
            pid_cls = resolve_class("raccoon.PidGains")
            self.imports.add(pid_cls)
            pid_args = ", ".join(
                f"{k}={build_literal_expr(v)}" for k, v in pid_cfg.items()
            )
            args.append(f"pid=PidGains({pid_args})")

        if ff_cfg:
            ff_cls = resolve_class("raccoon.Feedforward")
            self.imports.add(ff_cls)
            ff_args = ", ".join(
                f"{k}={build_literal_expr(v)}" for k, v in ff_cfg.items()
            )
            args.append(f"ff=Feedforward({ff_args})")

        return f"AxisVelocityControlConfig({', '.join(args)})"

    @staticmethod
    def _generate_vel_config_helper() -> str:
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

    def _build_odometry(
        self,
        odometry_cfg: Any,
        *,
        has_kinematics: bool,
        has_drive: bool,
    ) -> str:
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

        odometry_class = self.odometry_resolver.resolve_type(odometry_type)
        logger.info(f"Resolved odometry type '{odometry_type}' to {odometry_class.__name__}")
        init_params = self._introspect_odometry_params(odometry_class)

        self.imports.add(odometry_class)

        import inspect

        definitions = {}
        if hasattr(self, "_full_config"):
            definitions = self._full_config.get("definitions", {}) or {}

        reference_map = {
            "imu": "defs.imu",
            "defs": "defs",
        }
        if has_kinematics:
            reference_map["kinematics"] = "kinematics"
        if has_drive:
            reference_map["drive"] = "drive"
        for name in definitions:
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

        for key in sorted(set(params_cfg.keys()) - used_config_keys):
            if key not in init_params:
                logger.warning(
                    f"robot.odometry: Unknown parameter '{key}' for {odometry_class.__name__}"
                )

        args = [
            f"{name}={param_exprs[name]}"
            for name in init_params
            if name in param_exprs
        ]

        extra_keys = [key for key in used_config_keys if key not in init_params]
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
        if isinstance(value, str):
            if value in reference_map:
                return reference_map[value]
            if value.startswith("defs.") and value[5:] in reference_map:
                return reference_map[value[5:]]
            return build_literal_expr(value)

        if isinstance(value, dict) and odometry_class is not None and param_name is not None:
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

    def _introspect_odometry_params(self, odometry_class: type) -> Dict[str, Any]:
        from ..introspection import get_init_params
        return get_init_params(odometry_class)

    def generate_imports(self) -> str:
        generic_robot_cls = resolve_class("raccoon.GenericRobot")
        self.imports.add(generic_robot_cls)

        if hasattr(self, '_full_config'):
            robot_config = self._full_config.get("robot", {})
            physical = robot_config.get("physical", {})
            drive_config = robot_config.get("drive", {})
            kinematics = drive_config.get("kinematics", {})
            if physical.get("sensors") or kinematics:
                self.imports.add(resolve_class("raccoon.SensorPosition"))
                self.imports.add(resolve_class("raccoon.WheelPosition"))

        base_imports = super().generate_imports()

        defs_import = "from src.hardware.defs import Defs"

        mission_import_lines = [
            f"from src.missions.{m['file_name']} import {m['class_name']}"
            for m in self.mission_imports
        ]

        parts = []
        if base_imports:
            parts.append(base_imports)

        parts.append(defs_import)

        if mission_import_lines:
            parts.append("\n" + "\n".join(mission_import_lines))

        return "\n\n".join(parts)
