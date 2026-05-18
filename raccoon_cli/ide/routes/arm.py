"""ArmChain visualizer/editor endpoints — IDE backend side.

Serves the chain spec and kinematics for the Web-IDE 3D arm view, and
persists named positions back into the project YAML. The ``/command``
endpoint (which actually moves servos) lives on the Pi server, not here.

ikpy is imported lazily so the IDE backend boots without the ``[arm]``
extra installed; ImportError surfaces as HTTP 503 with a clear hint.
"""

from __future__ import annotations

import asyncio
import logging
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
from raccoon_cli.yaml_utils import _IncludeTag, load_yaml_raw, save_yaml_raw

log = logging.getLogger(__name__)

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


class JointSegmentSpec(BaseModel):
    origin_cm: list[float]
    end_cm: list[float]
    length_cm: float


class FKResponse(BaseModel):
    frames: list[list[float]]
    end_effector_cm: list[float]
    joint_axes: list[JointAxisSpec] = Field(default_factory=list)
    joint_segments: list[JointSegmentSpec] = Field(default_factory=list)


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


class JointStructureUpdate(BaseModel):
    """All fields optional — only provided keys are written back."""
    length_cm: Optional[float] = None
    axis: Optional[list[float]] = None
    mount_rpy_deg: Optional[list[float]] = None
    offset_cm: Optional[list[float]] = None  # use [] to clear (fall back to length-along-X)
    joint_range_deg: Optional[list[float]] = None
    servo_range_deg: Optional[list[float]] = None


class ArmStructureUpdate(BaseModel):
    """Patch joint structure and/or chain-level tip offset.

    ``joints`` length must match the existing joint count. To clear
    ``tip_offset_cm`` pass an empty list.
    """
    joints: Optional[list[JointStructureUpdate]] = None
    tip_offset_cm: Optional[list[float]] = None


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
    log.info("arm/chain: looking up project %s at %s", project_uuid, project_path)
    if not project_path.exists():
        log.warning("arm/chain: project path does not exist: %s", project_path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_uuid}' not found",
        )
    config = repo.read_project_config(project_uuid)
    if not config:
        log.warning("arm/chain: raccoon.project.yml missing or empty for %s", project_uuid)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"raccoon.project.yml not found or empty for '{project_uuid}'",
        )
    log.debug("arm/chain: config top-level keys: %s", list(config.keys()))
    definitions = config.get("definitions", {}) or {}
    log.info(
        "arm/chain: definitions keys: %s",
        {k: (v.get("type") if isinstance(v, dict) else type(v).__name__) for k, v in definitions.items()}
        if definitions else "(empty)",
    )
    for name, cfg in definitions.items():
        if isinstance(cfg, dict) and cfg.get("type") == "ArmChain":
            log.info("arm/chain: found ArmChain '%s'", name)
            return project_path, definitions, name, cfg
    log.warning(
        "arm/chain: no ArmChain found for project %s — definitions present: %s",
        project_uuid,
        list(definitions.keys()) or "(none)",
    )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"No ArmChain definition found in project definitions. "
            f"Definitions present: {list(definitions.keys()) or '(none)'}. "
            f"Check that raccoon.project.yml contains an entry with type: ArmChain."
        ),
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


def _locate_arm_leaf(
    path: Path,
    field_name: str,
    visited: Optional[set[Path]] = None,
    bound_key: Optional[str] = None,
):
    """Walk !include / !include-merge chain to find the physical YAML file
    that actually contains the arm definition.

    Returns ``(leaf_path, root_data, arm_block)`` — ``root_data`` is the full
    parsed tree to pass to ``save_yaml_raw`` and ``arm_block`` is the dict to
    mutate (a reference into ``root_data``).

    ``bound_key`` tracks the parent key this file was included under. When it
    equals ``field_name`` the included file's *contents* ARE the arm block
    (pattern: ``arm: !include arm.yml`` where arm.yml has ``type:`` /
    ``joints:`` at top level instead of an outer ``arm:`` key).
    """
    path = path.resolve()
    visited = visited if visited is not None else set()
    if path in visited or not path.exists():
        return None
    visited.add(path)

    data = load_yaml_raw(path)
    if not isinstance(data, dict):
        return None

    # File's contents ARE the arm block (bound by parent key).
    if bound_key == field_name:
        return (path, data, data)

    # File directly carries the arm key (e.g. file IS the definitions map).
    if field_name in data and isinstance(data[field_name], dict):
        return (path, data, data[field_name])

    # File has a `definitions:` mapping holding the arm.
    defs = data.get("definitions")
    if isinstance(defs, dict) and field_name in defs and isinstance(defs[field_name], dict):
        return (path, data, defs[field_name])

    def _recurse(container: dict):
        for k, v in container.items():
            if isinstance(v, _IncludeTag):
                inc_path = (path.parent / v.path).resolve()
                # `!include-merge` does not bind to a specific key — its content
                # merges into the parent. `!include` *does* bind the value to k.
                child_bound = k if v.tag == "!include" else None
                result = _locate_arm_leaf(inc_path, field_name, visited, child_bound)
                if result is not None:
                    return result
            elif isinstance(v, dict):
                result = _recurse(v)
                if result is not None:
                    return result
        return None

    return _recurse(data)


def _mutate_arm_block(project_path: Path, field_name: str, mutate) -> None:
    """Apply *mutate(arm_block)* on the YAML file that physically owns the arm
    definition, even when split across ``!include`` / ``!include-merge`` files.
    """
    root_yaml = project_path / "raccoon.project.yml"
    located = _locate_arm_leaf(root_yaml, field_name)
    if located is None:
        # Fallback to the legacy single-file resolver for direct layouts.
        target = resolve_config_file(project_path, "definitions")
        located = _locate_arm_leaf(target, field_name)
    if located is None:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not locate arm '{field_name}' in any project YAML file "
                f"(starting from {root_yaml.name}) for write-back. "
                f"Check that the arm definition lives in raccoon.project.yml or "
                f"in an !include / !include-merge'd file."
            ),
        )

    leaf_path, root_data, arm_block = located
    mutate(arm_block)
    save_yaml_raw(root_data, leaf_path)


def _write_position(project_path: Path, field_name: str, mutate) -> None:
    """Apply *mutate(positions_dict)* in the file that owns ``definitions``."""
    def _inner(arm_block):
        positions = arm_block.setdefault("positions", {})
        mutate(positions)
    _mutate_arm_block(project_path, field_name, _inner)


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
    chain, joint_mappings = _build_chain(hw_cfg, definitions, field_name)

    from raccoon_cli.codegen.arm.kinematics import (  # noqa: PLC0415
        end_effector_cm,
        forward_kinematics,
        joint_segments_cm,
        joint_world_axes,
    )

    frames = forward_kinematics(chain, request.joint_angles_deg)
    ee = end_effector_cm(chain, request.joint_angles_deg)
    axes = joint_world_axes(chain, request.joint_angles_deg)
    lengths = [float(m.get("length_cm", 0.0) or 0.0) for m in joint_mappings]
    segments = joint_segments_cm(chain, request.joint_angles_deg, lengths)
    return FKResponse(
        frames=frames,
        end_effector_cm=ee,
        joint_axes=[JointAxisSpec(**a) for a in axes],
        joint_segments=[JointSegmentSpec(**s) for s in segments],
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


@router.patch("/{project_uuid}/arm/structure", response_model=ArmChainSpec)
async def patch_arm_structure(
    project_uuid: UUID,
    request: ArmStructureUpdate,
    repo: ProjectRepository = Depends(get_project_repository),
):
    """Patch joint structural parameters and/or tip offset.

    Validates with a build_chain dry-run before writing. Invalidates the
    live-command cache so the next /command call rebuilds servo mappings.
    Returns the freshly reloaded chain spec.
    """
    project_path, definitions, field_name, hw_cfg = _load_arm(repo, project_uuid)
    joints_cfg = list(hw_cfg.get("joints", []) or [])

    if request.joints is not None and len(request.joints) != len(joints_cfg):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expected {len(joints_cfg)} joint updates, "
                f"got {len(request.joints)}"
            ),
        )

    def _apply_joint(target: dict, patch: JointStructureUpdate) -> None:
        if patch.length_cm is not None:
            if patch.length_cm < 0:
                raise HTTPException(status_code=400, detail="length_cm must be >= 0")
            target["length_cm"] = float(patch.length_cm)
        if patch.axis is not None:
            if len(patch.axis) != 3:
                raise HTTPException(status_code=400, detail="axis must have 3 components")
            target["axis"] = [float(v) for v in patch.axis]
        if patch.mount_rpy_deg is not None:
            if len(patch.mount_rpy_deg) != 3:
                raise HTTPException(status_code=400, detail="mount_rpy_deg must have 3 components")
            target["mount_rpy_deg"] = [float(v) for v in patch.mount_rpy_deg]
        if patch.offset_cm is not None:
            if len(patch.offset_cm) == 0:
                target.pop("offset_cm", None)
            elif len(patch.offset_cm) != 3:
                raise HTTPException(status_code=400, detail="offset_cm must have 3 components or be empty to clear")
            else:
                target["offset_cm"] = [float(v) for v in patch.offset_cm]
        if patch.joint_range_deg is not None:
            if len(patch.joint_range_deg) != 2:
                raise HTTPException(status_code=400, detail="joint_range_deg must be [lo, hi]")
            target["joint_range_deg"] = [float(v) for v in patch.joint_range_deg]
        if patch.servo_range_deg is not None:
            if len(patch.servo_range_deg) != 2:
                raise HTTPException(status_code=400, detail="servo_range_deg must be [lo, hi]")
            target["servo_range_deg"] = [float(v) for v in patch.servo_range_deg]

    # Dry-run validation: build a candidate config, run build_chain on it,
    # only persist if it succeeds.
    candidate_joints = [dict(j) for j in joints_cfg]
    if request.joints is not None:
        for jc, patch in zip(candidate_joints, request.joints):
            _apply_joint(jc, patch)

    candidate_tip = hw_cfg.get("tip_offset_cm")
    if request.tip_offset_cm is not None:
        if len(request.tip_offset_cm) == 0:
            candidate_tip = None
        elif len(request.tip_offset_cm) != 3:
            raise HTTPException(status_code=400, detail="tip_offset_cm must have 3 components or be empty to clear")
        else:
            candidate_tip = [float(v) for v in request.tip_offset_cm]

    try:
        from raccoon_cli.codegen.arm.kinematics import build_chain  # noqa: PLC0415
        build_chain(
            candidate_joints,
            definitions,
            field_name=field_name,
            tip_offset_cm=candidate_tip,
        )
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid structure: {exc}") from exc

    def _write(arm_block) -> None:
        if request.joints is not None:
            yaml_joints = arm_block.get("joints") or []
            for jc, patch in zip(yaml_joints, request.joints):
                _apply_joint(jc, patch)
        if request.tip_offset_cm is not None:
            if len(request.tip_offset_cm) == 0:
                arm_block.pop("tip_offset_cm", None)
            else:
                arm_block["tip_offset_cm"] = [float(v) for v in request.tip_offset_cm]

    _mutate_arm_block(project_path, field_name, _write)
    _live_cache.pop(str(project_uuid), None)

    # Re-issue the GET-chain logic so the caller gets the fresh spec.
    return await get_arm_chain(project_uuid, repo)  # type: ignore[return-value]


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
