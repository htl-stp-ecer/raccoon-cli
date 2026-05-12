"""Pure kinematics helpers for ArmChain definitions.

These functions are used both at codegen time (``arm_chain_generator``) and
from the FastAPI server routes that drive the 3D arm editor. They contain
only the math: workspace bounds and forbidden-zone checks live in the
codegen generator since they're authoring-time validations, not runtime
queries.

``ikpy`` is imported lazily so the toolchain works without the ``[arm]``
optional dependency until something actually asks for kinematics.
"""

from __future__ import annotations

import math
from typing import Any

_IKPY_MISSING_MSG = (
    "ArmChain kinematics require ikpy.\n"
    "Install it with: pip install 'raccoon-cli[arm]'\n"
    "  or: pip install ikpy"
)


def require_ikpy():
    """Import and return ``(ikpy_module, numpy_module)`` or raise ImportError."""
    try:
        import ikpy.chain  # noqa: PLC0415
        import ikpy.link  # noqa: PLC0415
        import numpy  # noqa: PLC0415

        return ikpy, numpy
    except ImportError as exc:
        raise ImportError(_IKPY_MISSING_MSG) from exc


def joint_to_servo_deg(
    joint_deg: float,
    joint_range: list[float],
    servo_range: list[float],
) -> float:
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


def build_chain(
    joints_cfg: list[dict[str, Any]],
    all_definitions: dict[str, Any],
    *,
    field_name: str = "arm",
):
    """Build an ikpy ``Chain`` plus per-joint metadata.

    Returns ``(chain, joint_mappings)`` where each entry in
    ``joint_mappings`` is::

        {
            "joint_range": [lo, hi],   # mathematical joint limits, deg
            "servo_range": [lo, hi],   # servo command range, deg
            "servo_ref":   "shoulder", # name of the Servo definition
            "axis":        [0, 1, 0],  # rotation axis
            "length_cm":   10.0,
        }

    The chain has an inactive OriginLink at index 0 and an inactive passive
    end-effector link at the end, matching the layout used by
    ``arm_chain_generator``.
    """
    if not joints_cfg:
        raise ValueError(f"definitions.{field_name}: 'joints' list is empty")

    ikpy_mod, _ = require_ikpy()

    chain_links = [ikpy_mod.link.OriginLink()]
    joint_mappings: list[dict[str, Any]] = []
    prev_translation = [0.0, 0.0, 0.0]

    for i, joint_cfg in enumerate(joints_cfg):
        length_cm = joint_cfg.get("length_cm")
        if length_cm is None:
            raise ValueError(
                f"definitions.{field_name}.joints[{i}]: missing 'length_cm'"
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
                f"definitions.{field_name}.joints[{i}]: missing 'servo' reference"
            )
        if servo_ref not in all_definitions:
            raise ValueError(
                f"definitions.{field_name}.joints[{i}]: "
                f"servo '{servo_ref}' not found in definitions"
            )

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
                "axis": axis,
                "length_cm": float(length_cm),
            }
        )

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
    return chain, joint_mappings


def _expand_active_angles(chain, joint_angles_deg: list[float]):
    """Convert per-joint degrees → full-chain radians vector."""
    _, np = require_ikpy()
    full = np.zeros(len(chain.links))
    # Active joints sit between OriginLink and end_effector.
    for i, deg in enumerate(joint_angles_deg):
        full[i + 1] = math.radians(deg)
    return full


def forward_kinematics(chain, joint_angles_deg: list[float]) -> list[list[float]]:
    """Return per-link frame translations in cm.

    The result has ``len(joints) + 1`` entries: the base origin followed by
    the tip of each joint's link. The final entry is the end-effector.
    """
    _, np = require_ikpy()
    full = _expand_active_angles(chain, joint_angles_deg)

    # ikpy exposes per-link transforms via forward_kinematics(..., full_kinematics=True).
    # Fallback: compute frame for prefix of each link manually.
    frames: list[list[float]] = []
    try:
        transforms = chain.forward_kinematics(full, full_kinematics=True)
        # transforms is a list of 4x4 matrices, one per link.
        # links: [Origin, joint_0, joint_1, ..., end_effector]
        # We want base (origin) + tip of each joint link = end of joint_i for i in 0..n-1
        # plus the end_effector frame. That equals len(joints)+1 entries (origin + each joint
        # tip ... last joint tip == end_effector frame because the end_effector link only
        # translates by the previous joint's length). To match "Basis + jedes Link-Ende"
        # we take Origin then every joint link tip, ending with the passive end_effector.
        # Concretely: indices 0, 2, 3, ..., n (skip OriginLink's redundant duplicate? no,
        # OriginLink == base, each joint link's frame is at its *tip*).
        # links[0] = base (origin), links[1..n] = joint tips, links[n+1] = end_effector tip.
        # We want: base, joint_0 tip, ..., joint_{n-1} tip, end_effector.
        # That's transforms[0], transforms[1], ..., transforms[-1].
        # But joint_{n-1} tip and end_effector are at the same Cartesian point because the
        # end_effector link has zero translation beyond prev_translation. To stay faithful
        # to the contract ("one entry per joint (Basis + jedes Link-Ende)"), drop the
        # duplicate end_effector frame and return base + each joint tip.
        for t in transforms[: -1]:
            xyz = t[:3, 3] * 100.0  # m → cm
            frames.append([float(xyz[0]), float(xyz[1]), float(xyz[2])])
        # Append end-effector explicitly (== last joint tip, but kept for clarity)
        ee = transforms[-1][:3, 3] * 100.0
        # If it matches the previous frame exactly, skip duplicate
        if not frames or any(
            abs(frames[-1][i] - float(ee[i])) > 1e-6 for i in range(3)
        ):
            frames.append([float(ee[0]), float(ee[1]), float(ee[2])])
    except TypeError:
        # Older ikpy without full_kinematics: fall back to per-prefix FK.
        n_active = sum(1 for _ in joint_angles_deg)
        # base frame
        base = chain.forward_kinematics(np.zeros(len(chain.links)))
        frames.append([float(base[0, 3] * 100), float(base[1, 3] * 100), float(base[2, 3] * 100)])
        for k in range(1, n_active + 1):
            partial = np.zeros(len(chain.links))
            for i in range(k):
                partial[i + 1] = full[i + 1]
            t = chain.forward_kinematics(partial)
            frames.append([float(t[0, 3] * 100), float(t[1, 3] * 100), float(t[2, 3] * 100)])

    return frames


def end_effector_cm(chain, joint_angles_deg: list[float]) -> list[float]:
    """Return the end-effector position in cm for the given joint angles."""
    full = _expand_active_angles(chain, joint_angles_deg)
    t = chain.forward_kinematics(full)
    return [float(t[0, 3] * 100), float(t[1, 3] * 100), float(t[2, 3] * 100)]


def inverse_kinematics(
    chain,
    target_xyz_cm: list[float],
    initial_angles_deg: list[float] | None = None,
) -> list[float]:
    """Solve IK and return active-joint angles in degrees (one per joint, no origin)."""
    _, np = require_ikpy()
    target_m = np.array([c / 100.0 for c in target_xyz_cm])

    kwargs: dict[str, Any] = {}
    if initial_angles_deg is not None:
        full = np.zeros(len(chain.links))
        for i, deg in enumerate(initial_angles_deg):
            full[i + 1] = math.radians(deg)
        kwargs["initial_position"] = full

    ik_result = chain.inverse_kinematics(target_m, **kwargs)
    # Active joints = indices 1..n (skip OriginLink and trailing end_effector).
    n_active = len(chain.links) - 2
    return [math.degrees(ik_result[i + 1]) for i in range(n_active)]
