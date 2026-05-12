"""ArmChain code-generation: solves IK at codegen time, emits angle literals.

IK runs here — on the dev machine — via ikpy. The generated defs.py contains
only pre-solved servo angles; the Wombat needs no IK library at runtime.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("raccoon")

# ikpy is a dev-only dependency (toolchain[arm]); imported lazily so that
# the rest of raccoon-cli works fine when ikpy is not installed.
_IKPY_MISSING_MSG = (
    "ArmChain codegen requires ikpy.\n"
    "Install it with: pip install 'raccoon-cli[arm]'\n"
    "  or: pip install ikpy"
)


def _require_ikpy():
    try:
        import ikpy.chain  # noqa: PLC0415
        import ikpy.link  # noqa: PLC0415
        import numpy  # noqa: PLC0415

        return ikpy, numpy
    except ImportError as exc:
        raise ImportError(_IKPY_MISSING_MSG) from exc


def _joint_to_servo(joint_deg: float, joint_range: list[float], servo_range: list[float]) -> float:
    """Linear 2-point map from mathematical joint angle to servo command angle.

    Handles both normal and inverted servos:
      normal:   servo_range=[10, 130], joint goes 0→90  → servo goes 10→130
      inverted: servo_range=[130, 10], joint goes 0→90  → servo goes 130→10
    """
    j_lo, j_hi = joint_range
    s_lo, s_hi = servo_range
    if j_hi == j_lo:
        return float(s_lo)
    t = (joint_deg - j_lo) / (j_hi - j_lo)
    return round(s_lo + t * (s_hi - s_lo), 2)


def _check_workspace(
    pos_name: str,
    x_cm: float,
    y_cm: float,
    z_cm: float,
    workspace: dict[str, Any],
) -> None:
    if "z_min_cm" in workspace and z_cm < workspace["z_min_cm"]:
        raise ValueError(
            f"arm.positions.{pos_name}: z={z_cm:.1f} cm violates "
            f"workspace.z_min_cm={workspace['z_min_cm']}"
        )
    if "z_max_cm" in workspace and z_cm > workspace["z_max_cm"]:
        raise ValueError(
            f"arm.positions.{pos_name}: z={z_cm:.1f} cm violates "
            f"workspace.z_max_cm={workspace['z_max_cm']}"
        )
    if "reach_max_cm" in workspace:
        r = math.sqrt(x_cm**2 + y_cm**2 + z_cm**2)
        if r > workspace["reach_max_cm"]:
            raise ValueError(
                f"arm.positions.{pos_name}: reach={r:.1f} cm violates "
                f"workspace.reach_max_cm={workspace['reach_max_cm']}"
            )


def _check_forbidden_zones(
    pos_name: str,
    joint_angles_deg: list[float],
    forbidden_zones: list[dict[str, Any]],
    joints_cfg: list[dict[str, Any]],
) -> None:
    for zone in forbidden_zones:
        zone_name = zone.get("name", "unnamed")
        condition = zone.get("condition", "").strip()
        if not condition:
            continue

        # Build eval context: {servo_name}_deg = angle and joint_{i}_deg = angle
        ctx: dict[str, float] = {}
        for i, (joint_cfg, angle) in enumerate(zip(joints_cfg, joint_angles_deg)):
            ctx[f"joint_{i}_deg"] = angle
            servo_ref = joint_cfg.get("servo", f"joint_{i}")
            ctx[f"{servo_ref}_deg"] = angle

        try:
            triggered = eval(condition, {"__builtins__": {}}, ctx)  # noqa: S307
        except Exception as exc:
            logger.warning(
                f"arm forbidden_zone '{zone_name}': could not evaluate "
                f"condition {condition!r}: {exc}"
            )
            continue

        if triggered:
            angle_map = {
                joint_cfg.get("servo", f"joint_{i}"): f"{a:.1f}°"
                for i, (joint_cfg, a) in enumerate(zip(joints_cfg, joint_angles_deg))
            }
            raise ValueError(
                f"arm.positions.{pos_name}: violates forbidden_zone '{zone_name}'\n"
                f"  condition: {condition}\n"
                f"  joint angles: {angle_map}"
            )


class ArmChainGenerator:
    """Generates an ArmPreset(...) expression with IK solved at codegen time.

    Called from DefsGenerator when it encounters ``type: ArmChain`` in the
    definitions block. The generator builds an ikpy chain from the joint
    configuration, solves IK for every named position, applies the per-joint
    linear servo mapping, and returns a Python expression string ready to be
    embedded in defs.py.
    """

    def __init__(
        self,
        field_name: str,
        hw_cfg: dict[str, Any],
        all_definitions: dict[str, Any],
        imports: Any,
    ) -> None:
        self.field_name = field_name
        self.hw_cfg = hw_cfg
        self.all_definitions = all_definitions
        self.imports = imports

    def build_expr(self) -> str:
        """Return the ``ArmPreset(...)`` constructor expression for defs.py."""
        ikpy_mod, np = _require_ikpy()

        joints_cfg: list[dict[str, Any]] = self.hw_cfg.get("joints", [])
        workspace: dict[str, Any] = self.hw_cfg.get("workspace", {})
        forbidden_zones: list[dict[str, Any]] = self.hw_cfg.get("forbidden_zones", [])
        positions: dict[str, Any] = self.hw_cfg.get("positions", {})

        if not joints_cfg:
            raise ValueError(f"definitions.{self.field_name}: 'joints' list is empty")

        # --- Build ikpy chain ---
        chain_links = [ikpy_mod.link.OriginLink()]
        joint_mappings: list[dict[str, Any]] = []
        prev_translation = [0.0, 0.0, 0.0]

        for i, joint_cfg in enumerate(joints_cfg):
            length_cm = joint_cfg.get("length_cm")
            if length_cm is None:
                raise ValueError(
                    f"definitions.{self.field_name}.joints[{i}]: missing 'length_cm'"
                )
            length_m = float(length_cm) / 100.0

            joint_range = list(joint_cfg.get("joint_range_deg", [0, 180]))
            servo_range = list(joint_cfg.get("servo_range_deg", joint_range))

            bounds = (
                math.radians(min(joint_range)),
                math.radians(max(joint_range)),
            )

            servo_ref = joint_cfg.get("servo")
            if servo_ref is None:
                raise ValueError(
                    f"definitions.{self.field_name}.joints[{i}]: missing 'servo' reference"
                )
            if servo_ref not in self.all_definitions:
                raise ValueError(
                    f"definitions.{self.field_name}.joints[{i}]: "
                    f"servo '{servo_ref}' not found in definitions"
                )

            # Default: Y-axis rotation → arm lifts in the X-Z plane (forward/up).
            # Override with 'axis: [0, 0, 1]' in the YAML for arms that swing sideways.
            axis = list(joint_cfg.get("axis", [0, 1, 0]))

            link = ikpy_mod.link.URDFLink(
                name=f"joint_{i}",
                origin_translation=prev_translation,
                origin_orientation=[0, 0, 0],
                rotation=axis,
                bounds=bounds,
            )
            chain_links.append(link)
            prev_translation = [length_m, 0.0, 0.0]
            joint_mappings.append(
                {
                    "joint_range": joint_range,
                    "servo_range": servo_range,
                    "servo_ref": servo_ref,
                }
            )

        # Passive end-effector link (provides the final frame offset)
        chain_links.append(
            ikpy_mod.link.URDFLink(
                name="end_effector",
                origin_translation=prev_translation,
                origin_orientation=[0, 0, 0],
                rotation=[0, 0, 0],
            )
        )

        active_mask = [False] + [True] * len(joints_cfg) + [False]
        chain = ikpy_mod.chain.Chain(chain_links, active_links_mask=active_mask)

        # --- Solve IK for each named position ---
        solved: dict[str, list[float]] = {}

        for pos_name, coords in positions.items():
            if not isinstance(coords, dict):
                raise ValueError(
                    f"definitions.{self.field_name}.positions.{pos_name}: "
                    f"expected {{x, y, z}} mapping, got {type(coords).__name__}"
                )
            x_m = float(coords.get("x", 0)) / 100.0
            y_m = float(coords.get("y", 0)) / 100.0
            z_m = float(coords.get("z", 0)) / 100.0

            _check_workspace(pos_name, x_m * 100, y_m * 100, z_m * 100, workspace)

            target_position = np.array([x_m, y_m, z_m])

            try:
                ik_result = chain.inverse_kinematics(target_position)
            except Exception as exc:
                raise ValueError(
                    f"arm.positions.{pos_name}: IK failed to converge: {exc}\n"
                    f"  target: x={x_m*100:.1f} cm, y={y_m*100:.1f} cm, z={z_m*100:.1f} cm\n"
                    f"  Check joint_range_deg limits and workspace bounds."
                ) from exc

            joint_angles_deg = [
                math.degrees(ik_result[i + 1]) for i in range(len(joints_cfg))
            ]

            _check_forbidden_zones(pos_name, joint_angles_deg, forbidden_zones, joints_cfg)

            servo_angles = [
                _joint_to_servo(
                    joint_angles_deg[i],
                    joint_mappings[i]["joint_range"],
                    joint_mappings[i]["servo_range"],
                )
                for i in range(len(joints_cfg))
            ]

            solved[pos_name] = servo_angles
            logger.info(
                f"arm.positions.{pos_name}: "
                f"joint_deg={[round(a, 1) for a in joint_angles_deg]} "
                f"→ servo_deg={servo_angles}"
            )

        # --- Emit ArmPreset(...) expression ---

        # Add ArmPreset to the import set without importing raccoon at codegen
        # time (raccoon._core is a compiled extension not available in the
        # toolchain's Python environment).  ImportSet.add() only reads
        # __module__ and __name__, so a lightweight namespace is sufficient.
        import types as _types  # noqa: PLC0415

        self.imports.add(
            _types.SimpleNamespace(
                __module__="raccoon.step.arm.preset",
                __name__="ArmPreset",
            )
        )

        joints_expr = "[" + ", ".join(
            f"{m['servo_ref']}.device" for m in joint_mappings
        ) + "]"

        positions_lines = [
            f'"{name}": [{", ".join(str(a) for a in angles)}]'
            for name, angles in solved.items()
        ]
        positions_expr = "{" + ", ".join(positions_lines) + "}"

        return f"ArmPreset(joints={joints_expr}, positions={positions_expr})"
