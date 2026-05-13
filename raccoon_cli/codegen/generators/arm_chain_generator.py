"""ArmChain code-generation: solves IK at codegen time, emits angle literals.

IK runs here — on the dev machine — via ikpy. The generated defs.py contains
only pre-solved servo angles; the Wombat needs no IK library at runtime.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from raccoon_cli.codegen.arm.kinematics import (
    build_chain,
    inverse_kinematics,
    joint_to_servo_deg,
    require_ikpy,
)

logger = logging.getLogger("raccoon")


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
        # Ensure ikpy is available (raises ImportError with a helpful hint).
        require_ikpy()

        joints_cfg: list[dict[str, Any]] = self.hw_cfg.get("joints", [])
        workspace: dict[str, Any] = self.hw_cfg.get("workspace", {})
        forbidden_zones: list[dict[str, Any]] = self.hw_cfg.get("forbidden_zones", [])
        positions: dict[str, Any] = self.hw_cfg.get("positions", {})

        chain, joint_mappings = build_chain(
            joints_cfg,
            self.all_definitions,
            field_name=self.field_name,
            tip_offset_cm=self.hw_cfg.get("tip_offset_cm"),
        )

        # --- Solve IK for each named position ---
        solved: dict[str, list[float]] = {}

        for pos_name, coords in positions.items():
            if not isinstance(coords, dict):
                raise ValueError(
                    f"definitions.{self.field_name}.positions.{pos_name}: "
                    f"expected {{x, y, z}} mapping, got {type(coords).__name__}"
                )
            x_cm = float(coords.get("x", 0))
            y_cm = float(coords.get("y", 0))
            z_cm = float(coords.get("z", 0))

            _check_workspace(pos_name, x_cm, y_cm, z_cm, workspace)

            try:
                joint_angles_deg = inverse_kinematics(chain, [x_cm, y_cm, z_cm])
            except Exception as exc:
                raise ValueError(
                    f"arm.positions.{pos_name}: IK failed to converge: {exc}\n"
                    f"  target: x={x_cm:.1f} cm, y={y_cm:.1f} cm, z={z_cm:.1f} cm\n"
                    f"  Check joint_range_deg limits and workspace bounds."
                ) from exc

            _check_forbidden_zones(pos_name, joint_angles_deg, forbidden_zones, joints_cfg)

            servo_angles = [
                joint_to_servo_deg(
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
