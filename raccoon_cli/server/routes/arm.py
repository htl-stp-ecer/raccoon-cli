"""ArmChain visualizer/editor endpoints.

Backs the Web-IDE 3D arm view: serves the chain spec (joints + named
positions), runs forward/inverse kinematics, commands servos directly via
``raccoon.hal``, and persists named positions back into
``raccoon.project.yml``.

Kinematics are intentionally lazy: ``ikpy`` is only imported when an endpoint
needs it, so the server boots fine without the ``[arm]`` extra installed.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from raccoon_cli.project import resolve_config_file
from raccoon_cli.server.auth import require_auth
from raccoon_cli.yaml_utils import load_yaml, load_yaml_raw, save_yaml_raw

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/arm",
    tags=["arm"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class JointSpec(BaseModel):
    index: int
    servo: str
    port: int
    length_cm: float
    axis: list[float]
    joint_range_deg: list[float]
    servo_range_deg: list[float]


class PositionSpec(BaseModel):
    joint_angles_deg: list[float]
    xyz_cm: list[float]


class ArmChainSpec(BaseModel):
    name: str
    joints: list[JointSpec]
    positions: dict[str, PositionSpec]
    workspace: dict[str, Any] = Field(default_factory=dict)
    forbidden_zones: list[dict[str, Any]] = Field(default_factory=list)


class FKRequest(BaseModel):
    joint_angles_deg: list[float]


class FKResponse(BaseModel):
    frames: list[list[float]]
    end_effector_cm: list[float]


class IKRequest(BaseModel):
    target_cm: list[float]
    initial_angles_deg: Optional[list[float]] = None


class IKResponse(BaseModel):
    joint_angles_deg: list[float]
    end_effector_cm: list[float]
    reachable: bool


class CommandRequest(BaseModel):
    joint_angles_deg: list[float]


class CommandedServo(BaseModel):
    servo: str
    port: int
    servo_deg: float


class CommandResponse(BaseModel):
    commanded: list[CommandedServo]
    success: bool


class PositionUpdateRequest(BaseModel):
    joint_angles_deg: list[float]


class PositionUpdateResponse(BaseModel):
    name: str
    joint_angles_deg: list[float]
    xyz_cm: list[float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_yaml_path(project_id: str) -> Path:
    from raccoon_cli.server.app import get_config  # noqa: PLC0415
    from raccoon_cli.server.services.project_manager import ProjectManager  # noqa: PLC0415

    manager = ProjectManager(get_config().projects_dir)
    project_path = manager.get_project_path(project_id)
    if project_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found",
        )
    yml = project_path / "raccoon.project.yml"
    if not yml.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"raccoon.project.yml not found for project '{project_id}'",
        )
    return yml


def _find_arm_chain(definitions: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(field_name, hw_cfg)`` for the first ArmChain in *definitions*.

    Raises HTTPException(404) when none exists.
    """
    for name, cfg in definitions.items():
        if isinstance(cfg, dict) and cfg.get("type") == "ArmChain":
            return name, cfg
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No ArmChain definition found in raccoon.project.yml",
    )


def _load_arm(project_id: str):
    """Load YAML and return ``(yml_path, data, field_name, hw_cfg, definitions)``.

    Uses the resolving loader so ``!include`` / ``!include-merge`` directives
    (e.g. ``definitions: !include servos.yml``) are followed transparently.
    """
    yml_path = _project_yaml_path(project_id)
    data = load_yaml(yml_path)
    definitions = data.get("definitions", {}) or {}
    field_name, hw_cfg = _find_arm_chain(definitions)
    return yml_path, data, field_name, hw_cfg, definitions


def _write_position(main_yml: Path, field_name: str, mutate) -> None:
    """Apply *mutate(positions_dict)* in the file that owns ``definitions``.

    Uses :func:`resolve_config_file` so writes land in the actual source
    file — whether that's the main project YAML, a ``!include`` target,
    or an ``!include-merge`` source.
    """
    target = resolve_config_file(main_yml.parent, "definitions")
    data = load_yaml_raw(target)

    # Two shapes are possible:
    #   1. target is the included file itself (`definitions: !include x.yml`)
    #      → its root mapping IS the definitions block.
    #   2. target carries a top-level `definitions:` key (main YAML or
    #      include-merge source).
    container = data
    if isinstance(container, dict) and "definitions" in container and field_name not in container:
        container = container["definitions"]

    if not isinstance(container, dict) or field_name not in container:
        raise HTTPException(
            status_code=500,
            detail=f"Could not locate arm '{field_name}' in {target.name} for write-back",
        )

    arm_block = container[field_name]
    positions = arm_block.setdefault("positions", {})
    mutate(positions)
    save_yaml_raw(data, target)


def _build_chain_or_503(hw_cfg: dict[str, Any], definitions: dict[str, Any], field_name: str):
    """Build chain, translating ImportError → HTTP 503."""
    try:
        from raccoon_cli.codegen.arm.kinematics import build_chain  # noqa: PLC0415

        return build_chain(
            hw_cfg.get("joints", []), definitions, field_name=field_name
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/chain", response_model=ArmChainSpec)
async def get_arm_chain(project_id: str):
    """Return the ArmChain spec with joints and named positions."""
    _, _, field_name, hw_cfg, definitions = _load_arm(project_id)
    chain, joint_mappings = _build_chain_or_503(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import (  # noqa: PLC0415
        end_effector_cm,
        inverse_kinematics,
    )

    joints: list[JointSpec] = []
    for i, m in enumerate(joint_mappings):
        servo_def = definitions.get(m["servo_ref"], {})
        port = int(servo_def.get("port", 0)) if isinstance(servo_def, dict) else 0
        joints.append(
            JointSpec(
                index=i,
                servo=m["servo_ref"],
                port=port,
                length_cm=m["length_cm"],
                axis=[float(a) for a in m["axis"]],
                joint_range_deg=[float(v) for v in m["joint_range"]],
                servo_range_deg=[float(v) for v in m["servo_range"]],
            )
        )

    positions_out: dict[str, PositionSpec] = {}
    raw_positions = hw_cfg.get("positions", {}) or {}
    for pos_name, coords in raw_positions.items():
        if not isinstance(coords, dict):
            continue
        x_cm = float(coords.get("x", 0))
        y_cm = float(coords.get("y", 0))
        z_cm = float(coords.get("z", 0))
        try:
            angles = inverse_kinematics(chain, [x_cm, y_cm, z_cm])
        except Exception:
            # If a stored position no longer solves cleanly, expose zeros for
            # angles rather than blowing up the whole listing.
            angles = [0.0] * len(joint_mappings)
        positions_out[pos_name] = PositionSpec(
            joint_angles_deg=[round(a, 4) for a in angles],
            xyz_cm=[x_cm, y_cm, z_cm],
        )

    return ArmChainSpec(
        name=field_name,
        joints=joints,
        positions=positions_out,
        workspace=dict(hw_cfg.get("workspace", {}) or {}),
        forbidden_zones=list(hw_cfg.get("forbidden_zones", []) or []),
    )


@router.post("/fk", response_model=FKResponse)
async def compute_fk(project_id: str, request: FKRequest):
    """Forward kinematics: joint angles → per-link frames + end effector."""
    _, _, field_name, hw_cfg, definitions = _load_arm(project_id)
    chain, _ = _build_chain_or_503(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import (  # noqa: PLC0415
        end_effector_cm,
        forward_kinematics,
    )

    frames = forward_kinematics(chain, request.joint_angles_deg)
    ee = end_effector_cm(chain, request.joint_angles_deg)
    return FKResponse(frames=frames, end_effector_cm=ee)


@router.post("/ik", response_model=IKResponse)
async def compute_ik(project_id: str, request: IKRequest):
    """Inverse kinematics: target xyz → joint angles + reachability."""
    _, _, field_name, hw_cfg, definitions = _load_arm(project_id)
    chain, _ = _build_chain_or_503(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import (  # noqa: PLC0415
        end_effector_cm,
        inverse_kinematics,
    )

    try:
        angles = inverse_kinematics(
            chain, request.target_cm, initial_angles_deg=request.initial_angles_deg
        )
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"IK failed: {exc}") from exc

    ee = end_effector_cm(chain, angles)
    dx = ee[0] - request.target_cm[0]
    dy = ee[1] - request.target_cm[1]
    dz = ee[2] - request.target_cm[2]
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)

    return IKResponse(
        joint_angles_deg=[round(a, 4) for a in angles],
        end_effector_cm=ee,
        reachable=dist <= 1.0,
    )


@router.post("/command", response_model=CommandResponse)
async def command_servos(project_id: str, request: CommandRequest):
    """Map joint angles to servo angles and command them via raccoon.hal."""
    _, _, field_name, hw_cfg, definitions = _load_arm(project_id)
    _, joint_mappings = _build_chain_or_503(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import joint_to_servo_deg  # noqa: PLC0415

    if len(request.joint_angles_deg) != len(joint_mappings):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expected {len(joint_mappings)} joint angles, "
                f"got {len(request.joint_angles_deg)}"
            ),
        )

    try:
        from raccoon.hal import Servo as HalServo  # type: ignore  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "raccoon not available on this system. "
                "Hardware access requires running on the Pi."
            ),
        ) from exc

    commanded: list[CommandedServo] = []
    for i, joint_deg in enumerate(request.joint_angles_deg):
        m = joint_mappings[i]
        servo_def = definitions.get(m["servo_ref"], {})
        if not isinstance(servo_def, dict) or "port" not in servo_def:
            raise HTTPException(
                status_code=400,
                detail=f"Servo '{m['servo_ref']}' has no 'port' in definitions",
            )
        port = int(servo_def["port"])
        servo_deg = joint_to_servo_deg(
            float(joint_deg), m["joint_range"], m["servo_range"]
        )
        servo = HalServo(port=port)
        servo.set_position(float(servo_deg))
        commanded.append(
            CommandedServo(servo=m["servo_ref"], port=port, servo_deg=servo_deg)
        )

    return CommandResponse(commanded=commanded, success=True)


@router.put("/positions/{name}", response_model=PositionUpdateResponse)
async def upsert_position(project_id: str, name: str, request: PositionUpdateRequest):
    """Compute FK for the given joint angles and persist the xyz position."""
    yml_path, data, field_name, hw_cfg, definitions = _load_arm(project_id)
    chain, joint_mappings = _build_chain_or_503(hw_cfg, definitions, field_name)

    if len(request.joint_angles_deg) != len(joint_mappings):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expected {len(joint_mappings)} joint angles, "
                f"got {len(request.joint_angles_deg)}"
            ),
        )

    from raccoon_cli.codegen.arm.kinematics import end_effector_cm  # noqa: PLC0415

    xyz = end_effector_cm(chain, request.joint_angles_deg)
    x_cm, y_cm, z_cm = (round(v, 4) for v in xyz)

    def _set(positions: dict) -> None:
        positions[name] = {"x": x_cm, "y": y_cm, "z": z_cm}

    _write_position(yml_path, field_name, _set)

    return PositionUpdateResponse(
        name=name,
        joint_angles_deg=[round(a, 4) for a in request.joint_angles_deg],
        xyz_cm=[x_cm, y_cm, z_cm],
    )


@router.delete("/positions/{name}")
async def delete_position(project_id: str, name: str):
    """Remove a named position from the ArmChain YAML block."""
    yml_path, _, field_name, hw_cfg, _ = _load_arm(project_id)

    existing = (hw_cfg.get("positions") or {})
    if name not in existing:
        raise HTTPException(
            status_code=404,
            detail=f"Position '{name}' not found in arm '{field_name}'",
        )

    def _del(positions: dict) -> None:
        positions.pop(name, None)

    _write_position(yml_path, field_name, _del)
    return {"status": "deleted", "name": name}
