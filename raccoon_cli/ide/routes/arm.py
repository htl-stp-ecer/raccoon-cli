"""ArmChain visualizer/editor endpoints — IDE backend side.

Serves the chain spec and kinematics for the Web-IDE 3D arm view, and
persists named positions back into the project YAML. The ``/command``
endpoint (which actually moves servos) lives on the Pi server, not here.

ikpy is imported lazily so the IDE backend boots without the ``[arm]``
extra installed; ImportError surfaces as HTTP 503 with a clear hint.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from raccoon_cli.ide.repositories.project_repository import ProjectRepository
from raccoon_cli.project import resolve_config_file
from raccoon_cli.yaml_utils import load_yaml_raw, save_yaml_raw

router = APIRouter()

# ---------------------------------------------------------------------------
# Live-command cache — avoids re-reading YAML and re-fetching token on every
# call during live preview, keeping latency down to just angle math + one
# fire-and-forget network write.
# ---------------------------------------------------------------------------

@dataclass
class _LiveCache:
    pi_base: str
    token: str
    token_ts: float  # monotonic timestamp of last token fetch
    mappings: list[dict]  # [{port, joint_range, servo_range}] per joint

_live_cache: dict[str, _LiveCache] = {}   # str(project_uuid) → cache
_pending_tasks: dict[str, asyncio.Task] = {}  # str(project_uuid) → in-flight task
_TOKEN_TTL = 300.0  # seconds before re-fetching the token


def get_project_repository() -> ProjectRepository:
    """Dependency injection — overridden by app factory."""
    raise NotImplementedError("ProjectRepository dependency not configured")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class JointSpec(BaseModel):
    index: int
    servo: str
    port: int
    length_cm: float
    axis: list[float]
    mount_rpy_deg: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    offset_cm: Optional[list[float]] = None
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
    tip_offset_cm: Optional[list[float]] = None


class FKRequest(BaseModel):
    joint_angles_deg: list[float]


class JointAxisSpec(BaseModel):
    origin_cm: list[float]
    axis: list[float]


class FKResponse(BaseModel):
    frames: list[list[float]]
    end_effector_cm: list[float]
    joint_axes: list[JointAxisSpec] = Field(default_factory=list)


class IKRequest(BaseModel):
    target_cm: list[float]
    initial_angles_deg: Optional[list[float]] = None
    # 0-based index of the last joint allowed to move. Lets the UI drag an
    # intermediate joint pivot and solve only the joints before it, keeping
    # the rest pinned at initial_angles_deg. Omit / null → full chain.
    end_joint_index: Optional[int] = None


class IKResponse(BaseModel):
    joint_angles_deg: list[float]
    end_effector_cm: list[float]
    reachable: bool


class CommandRequest(BaseModel):
    joint_angles_deg: list[float]


class CommandResponse(BaseModel):
    success: bool
    count: int


class PositionUpdateRequest(BaseModel):
    joint_angles_deg: list[float]


class PositionUpdateResponse(BaseModel):
    name: str
    joint_angles_deg: list[float]
    xyz_cm: list[float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_arm(repo: ProjectRepository, project_uuid: UUID):
    """Resolve project YAML and locate the ArmChain definition.

    Returns ``(project_path, definitions, field_name, hw_cfg)``. Raises 404
    distinct messages for "no project", "no config", and "no arm" so the
    caller can tell them apart.
    """
    project_path = repo.get_project_path(project_uuid)
    if not project_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_uuid}' not found",
        )
    config = repo.read_project_config(project_uuid)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"raccoon.project.yml not found or empty for '{project_uuid}'",
        )
    definitions = config.get("definitions", {}) or {}
    for name, cfg in definitions.items():
        if isinstance(cfg, dict) and cfg.get("type") == "ArmChain":
            return project_path, definitions, name, cfg
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No ArmChain definition found in project definitions",
    )


def _build_chain(hw_cfg: dict[str, Any], definitions: dict[str, Any], field_name: str):
    """Build ikpy chain. ImportError → 503, ValueError → 400."""
    try:
        from raccoon_cli.codegen.arm.kinematics import build_chain  # noqa: PLC0415

        return build_chain(
            hw_cfg.get("joints", []),
            definitions,
            field_name=field_name,
            tip_offset_cm=hw_cfg.get("tip_offset_cm"),
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"ArmChain kinematics require ikpy. Install with "
                f"`pip install 'raccoon-cli[arm]'`. ({exc})"
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _write_position(project_path: Path, field_name: str, mutate) -> None:
    """Apply *mutate(positions_dict)* in the file that owns ``definitions``."""
    target = resolve_config_file(project_path, "definitions")
    data = load_yaml_raw(target)

    # Either the target file IS the definitions mapping (when included as
    # `definitions: !include …`), or it carries a top-level `definitions:`
    # key (main YAML or include-merge source).
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{project_uuid}/arm/chain", response_model=ArmChainSpec)
async def get_arm_chain(
    project_uuid: UUID,
    repo: ProjectRepository = Depends(get_project_repository),
):
    """Return the ArmChain spec with joints and named positions."""
    _, definitions, field_name, hw_cfg = _load_arm(repo, project_uuid)
    chain, joint_mappings = _build_chain(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import inverse_kinematics  # noqa: PLC0415

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
                mount_rpy_deg=[float(v) for v in m.get("mount_rpy_deg", [0, 0, 0])],
                offset_cm=(
                    [float(v) for v in m["offset_cm"]]
                    if m.get("offset_cm") is not None
                    else None
                ),
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
            angles = [0.0] * len(joint_mappings)
        positions_out[pos_name] = PositionSpec(
            joint_angles_deg=[round(a, 4) for a in angles],
            xyz_cm=[x_cm, y_cm, z_cm],
        )

    tip = hw_cfg.get("tip_offset_cm")
    return ArmChainSpec(
        name=field_name,
        joints=joints,
        positions=positions_out,
        workspace=dict(hw_cfg.get("workspace", {}) or {}),
        forbidden_zones=list(hw_cfg.get("forbidden_zones", []) or []),
        tip_offset_cm=[float(v) for v in tip] if tip is not None else None,
    )


@router.post("/{project_uuid}/arm/fk", response_model=FKResponse)
async def compute_fk(
    project_uuid: UUID,
    request: FKRequest,
    repo: ProjectRepository = Depends(get_project_repository),
):
    _, definitions, field_name, hw_cfg = _load_arm(repo, project_uuid)
    chain, _ = _build_chain(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import (  # noqa: PLC0415
        end_effector_cm,
        forward_kinematics,
        joint_world_axes,
    )

    frames = forward_kinematics(chain, request.joint_angles_deg)
    ee = end_effector_cm(chain, request.joint_angles_deg)
    axes = joint_world_axes(chain, request.joint_angles_deg)
    return FKResponse(
        frames=frames,
        end_effector_cm=ee,
        joint_axes=[JointAxisSpec(**a) for a in axes],
    )


@router.post("/{project_uuid}/arm/ik", response_model=IKResponse)
async def compute_ik(
    project_uuid: UUID,
    request: IKRequest,
    repo: ProjectRepository = Depends(get_project_repository),
):
    _, definitions, field_name, hw_cfg = _load_arm(repo, project_uuid)

    from raccoon_cli.codegen.arm.kinematics import (  # noqa: PLC0415
        build_chain,
        end_effector_cm,
        inverse_kinematics,
    )

    joints_cfg = hw_cfg.get("joints", []) or []
    n_total = len(joints_cfg)
    initial = list(request.initial_angles_deg or [0.0] * n_total)
    if len(initial) < n_total:
        initial = initial + [0.0] * (n_total - len(initial))

    # Decide whether to solve the full chain or a prefix (sub-chain IK for
    # dragging an intermediate joint pivot). end_joint_index is the *index of
    # the last joint that may move*, so the sub-chain runs joints[0..end].
    end_idx = request.end_joint_index
    if end_idx is None or end_idx >= n_total - 1:
        chain, _ = _build_chain(hw_cfg, definitions, field_name)
        solve_initial: Optional[list[float]] = request.initial_angles_deg
        n_active = n_total
    else:
        if end_idx < 0:
            raise HTTPException(
                status_code=400,
                detail=f"end_joint_index must be >= 0, got {end_idx}",
            )
        try:
            # The sub-chain ends at joint end_idx's frame; the user is dragging
            # joint end_idx+1's pivot. If the next joint declares offset_cm we
            # use it as the tip offset so the IK target lines up with the
            # actual rendered pivot; otherwise build_chain applies its legacy
            # length-of-end_idx-along-X fallback automatically.
            next_joint = joints_cfg[end_idx + 1] if end_idx + 1 < n_total else {}
            sub_tip = next_joint.get("offset_cm")
            chain, _ = build_chain(
                joints_cfg[: end_idx + 1],
                definitions,
                field_name=field_name,
                tip_offset_cm=[float(v) for v in sub_tip] if sub_tip else None,
            )
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        solve_initial = initial[: end_idx + 1]
        n_active = end_idx + 1

    try:
        solved = inverse_kinematics(
            chain, request.target_cm, initial_angles_deg=solve_initial
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"IK failed: {exc}") from exc

    full_angles = list(solved) + list(initial[n_active:])

    ee_xyz = end_effector_cm(chain, solved)
    dist = math.sqrt(sum((ee_xyz[i] - request.target_cm[i]) ** 2 for i in range(3)))

    # The "end_effector" reported to the client should be the *full* arm's tip
    # so the visualizer can update consistently. Rebuild the full chain only
    # when we actually solved a sub-chain.
    if n_active < n_total:
        full_chain, _ = _build_chain(hw_cfg, definitions, field_name)
        ee_full = end_effector_cm(full_chain, full_angles)
    else:
        ee_full = ee_xyz

    return IKResponse(
        joint_angles_deg=[round(a, 4) for a in full_angles],
        end_effector_cm=ee_full,
        reachable=dist <= 1.0,
    )


async def _send_servo_positions(pi_base: str, token: str, positions: list[dict]) -> None:
    """Fire-and-forget: send servo positions to the Pi. Errors are swallowed."""
    import httpx  # noqa: PLC0415
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{pi_base}/api/v1/servo/set",
                json={"positions": positions},
                headers={"X-API-Token": token} if token else {},
            )
    except Exception:
        pass


async def _refresh_token(pi_base: str) -> str:
    import httpx  # noqa: PLC0415
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{pi_base}/api/v1/device/token")
            return resp.json().get("token", "")
    except Exception:
        return ""


@router.post("/{project_uuid}/arm/command", response_model=CommandResponse, status_code=202)
async def command_arm(
    project_uuid: UUID,
    request: CommandRequest,
    repo: ProjectRepository = Depends(get_project_repository),
):
    """Convert joint angles → servo angles and forward to the Pi — non-blocking.

    Returns 202 immediately. The Pi network call happens in a background task
    so live-preview dragging stays responsive. Only the latest in-flight command
    is kept; a superseded one is cancelled to avoid servo command queuing.
    """
    import httpx  # noqa: PLC0415
    from raccoon_cli.codegen.arm.kinematics import joint_to_servo_deg  # noqa: PLC0415

    cache_key = str(project_uuid)
    cache = _live_cache.get(cache_key)

    # (Re)build cache on first call or when YAML might have changed
    if cache is None:
        _, definitions, field_name, hw_cfg = _load_arm(repo, project_uuid)
        joints_cfg = hw_cfg.get("joints", []) or []

        config = repo.read_project_config(project_uuid)
        conn = config.get("connection") or {}
        pi_address = conn.get("pi_address")
        pi_port = int(conn.get("pi_port") or 8421)
        if not pi_address:
            raise HTTPException(status_code=424, detail="No pi_address configured for this project.")
        pi_base = f"http://{pi_address}:{pi_port}"

        mappings = []
        for jcfg in joints_cfg:
            servo_ref = jcfg.get("servo_ref") or jcfg.get("servo", "")
            servo_def = definitions.get(servo_ref, {})
            if not isinstance(servo_def, dict) or "port" not in servo_def:
                raise HTTPException(status_code=400, detail=f"Servo '{servo_ref}' has no 'port'")
            mappings.append({
                "port": int(servo_def["port"]),
                "joint_range": jcfg.get("joint_range", [-90, 90]),
                "servo_range": jcfg.get("servo_range", [0, 180]),
            })

        token = await _refresh_token(pi_base)
        cache = _LiveCache(pi_base=pi_base, token=token, token_ts=time.monotonic(), mappings=mappings)
        _live_cache[cache_key] = cache
    else:
        # Refresh token if stale
        if time.monotonic() - cache.token_ts > _TOKEN_TTL:
            cache.token = await _refresh_token(cache.pi_base)
            cache.token_ts = time.monotonic()

    if len(request.joint_angles_deg) != len(cache.mappings):
        # Config changed — invalidate cache and let next call rebuild
        _live_cache.pop(cache_key, None)
        raise HTTPException(status_code=400, detail="Joint count mismatch — retrying will rebuild cache.")

    positions = [
        {"port": m["port"], "angle_deg": joint_to_servo_deg(float(a), m["joint_range"], m["servo_range"])}
        for a, m in zip(request.joint_angles_deg, cache.mappings)
    ]

    # Cancel the previous in-flight task so we never queue up stale positions
    prev = _pending_tasks.get(cache_key)
    if prev and not prev.done():
        prev.cancel()
    _pending_tasks[cache_key] = asyncio.create_task(
        _send_servo_positions(cache.pi_base, cache.token, positions)
    )

    return CommandResponse(success=True, count=len(positions))


@router.put("/{project_uuid}/arm/positions/{name}", response_model=PositionUpdateResponse)
async def upsert_position(
    project_uuid: UUID,
    name: str,
    request: PositionUpdateRequest,
    repo: ProjectRepository = Depends(get_project_repository),
):
    project_path, definitions, field_name, hw_cfg = _load_arm(repo, project_uuid)
    chain, joint_mappings = _build_chain(hw_cfg, definitions, field_name)

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

    _write_position(project_path, field_name, _set)
    _live_cache.pop(str(project_uuid), None)  # force re-read on next command

    return PositionUpdateResponse(
        name=name,
        joint_angles_deg=[round(a, 4) for a in request.joint_angles_deg],
        xyz_cm=[x_cm, y_cm, z_cm],
    )


@router.delete("/{project_uuid}/arm/positions/{name}")
async def delete_position(
    project_uuid: UUID,
    name: str,
    repo: ProjectRepository = Depends(get_project_repository),
):
    project_path, _, field_name, hw_cfg = _load_arm(repo, project_uuid)
    existing = hw_cfg.get("positions") or {}
    if name not in existing:
        raise HTTPException(
            status_code=404,
            detail=f"Position '{name}' not found in arm '{field_name}'",
        )

    def _del(positions: dict) -> None:
        positions.pop(name, None)

    _write_position(project_path, field_name, _del)
    return {"status": "deleted", "name": name}
