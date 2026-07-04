"""Device configuration endpoints for local IDE (project-scoped physical settings)."""

from typing import Callable, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from raccoon_cli.ide.repositories.project_repository import ProjectRepository
from raccoon_cli.ide.services.project_service import ProjectService
from raccoon_cli.table_map import TableMapRequest, TableMapVersionError, parse_v2


router = APIRouter()


# =============================================================================
# Pydantic Models (matching the server/routes/device.py models)
# =============================================================================


class SensorInfo(BaseModel):
    """Sensor position information in cm from lower-left origin."""

    name: str
    x_cm: Optional[float] = None  # Distance from left edge
    y_cm: Optional[float] = None  # Distance from back edge (0=back, length=front)
    clearance_cm: Optional[float] = None


class CenterPoint(BaseModel):
    """Center point position in cm from lower-left origin."""

    x_cm: float  # Distance from left edge
    y_cm: float  # Distance from back edge


class StartPose(BaseModel):
    """Starting pose on the table."""

    x_cm: float
    y_cm: float
    theta_deg: float


class DeviceInfo(BaseModel):
    """Device/physical info for a project (local version of ConnectionInfo)."""

    hostname: str = "local"
    ip: str = "127.0.0.1"
    battery_voltage_v: Optional[float] = None
    battery_percent: Optional[float] = None
    width_cm: Optional[float] = None
    length_cm: Optional[float] = None
    sensors: Optional[List[SensorInfo]] = None
    rotation_center: Optional[CenterPoint] = None
    start_pose: Optional[StartPose] = None
    # Kinematics info (editable, from robot.drive.kinematics)
    drive_type: Optional[str] = None
    track_width_m: Optional[float] = None
    wheelbase_m: Optional[float] = None
    wheel_radius_m: Optional[float] = None


class DimensionsRequest(BaseModel):
    """Request to update robot dimensions."""

    width_cm: float
    length_cm: float


class SensorsRequest(BaseModel):
    """Request to update sensor positions."""

    sensors: List[SensorInfo]


class RotationCenterRequest(BaseModel):
    """Request to update rotation center."""

    rotation_center: Optional[CenterPoint] = None


class StartPoseRequest(BaseModel):
    """Request to update start pose."""

    start_pose: StartPose


class KinematicsRequest(BaseModel):
    """Request to update kinematics (track width, wheelbase, wheel radius)."""

    track_width_m: Optional[float] = None
    wheelbase_m: Optional[float] = None
    wheel_radius_m: Optional[float] = None


# =============================================================================
# Dependency Injection
# =============================================================================


def get_project_service() -> ProjectService:
    """Dependency injection for ProjectService - will be overridden by app."""
    raise NotImplementedError("ProjectService dependency not configured")


# =============================================================================
# Helper Functions
# =============================================================================


def _build_device_info(config: dict) -> DeviceInfo:
    """Build DeviceInfo from project config."""
    robot_config = config.get("robot", {})
    physical = robot_config.get("physical", {})

    # Physical dimensions
    width_cm = physical.get("width_cm")
    length_cm = physical.get("length_cm")

    # Sensors (stored in cm from lower-left origin)
    sensors: Optional[List[SensorInfo]] = None
    sensor_list = physical.get("sensors", [])
    if sensor_list:
        sensors = [
            SensorInfo(
                name=s.get("name", ""),
                x_cm=s.get("x_cm"),
                y_cm=s.get("y_cm"),
                clearance_cm=s.get("clearance_cm"),
            )
            for s in sensor_list
        ]

    # Rotation center (stored in cm from lower-left origin)
    rotation_center: Optional[CenterPoint] = None
    rc = physical.get("rotation_center")
    if rc and "x_cm" in rc and "y_cm" in rc:
        rotation_center = CenterPoint(x_cm=rc.get("x_cm"), y_cm=rc.get("y_cm"))

    # Start pose
    start_pose: Optional[StartPose] = None
    sp = physical.get("start_pose")
    if sp:
        start_pose = StartPose(
            x_cm=sp.get("x_cm", 0),
            y_cm=sp.get("y_cm", 0),
            theta_deg=sp.get("theta_deg", 0),
        )

    # Kinematics (editable)
    drive_config = robot_config.get("drive", {})
    kinematics = drive_config.get("kinematics", {})
    drive_type = kinematics.get("type")
    track_width_m = kinematics.get("track_width")
    wheelbase_m = kinematics.get("wheelbase")
    wheel_radius_m = kinematics.get("wheel_radius")

    return DeviceInfo(
        hostname="local",
        ip="127.0.0.1",
        width_cm=width_cm,
        length_cm=length_cm,
        sensors=sensors,
        rotation_center=rotation_center,
        start_pose=start_pose,
        drive_type=drive_type,
        track_width_m=track_width_m,
        wheelbase_m=wheelbase_m,
        wheel_radius_m=wheel_radius_m,
    )


def _ensure_physical_section(config: dict) -> dict:
    """Ensure the robot.physical section exists in config."""
    if "robot" not in config:
        config["robot"] = {}
    if "physical" not in config["robot"]:
        config["robot"]["physical"] = {}
    return config


def _update_robot_config(
    project_uuid: UUID,
    svc: ProjectService,
    mutate: Callable[[dict], None],
) -> dict | None:
    """Apply a mutation to ``config['robot']`` and persist only that top-level key."""
    config = svc.project_repository.read_project_config(project_uuid)
    if not config:
        return None

    if "robot" not in config or not isinstance(config["robot"], dict):
        config["robot"] = {}
    mutate(config["robot"])
    svc.project_repository.save_config_keys(project_uuid, {"robot": config["robot"]})
    return config


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/{project_uuid}/info", response_model=DeviceInfo)
async def get_device_info(
    project_uuid: UUID,
    svc: ProjectService = Depends(get_project_service),
):
    """Get device/physical info for a project."""
    config = svc.project_repository.read_project_config(project_uuid)
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _build_device_info(config)


@router.put("/{project_uuid}/dimensions", response_model=DeviceInfo)
async def update_dimensions(
    project_uuid: UUID,
    request: DimensionsRequest,
    svc: ProjectService = Depends(get_project_service),
):
    """Update robot dimensions for a project."""
    if request.width_cm <= 0 or request.length_cm <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dimensions must be positive")

    def mutate(robot_config: dict) -> None:
        robot_config.setdefault("physical", {})
        robot_config["physical"]["width_cm"] = request.width_cm
        robot_config["physical"]["length_cm"] = request.length_cm

    updated = _update_robot_config(project_uuid, svc, mutate)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _build_device_info(updated)


@router.put("/{project_uuid}/sensors", response_model=DeviceInfo)
async def update_sensors(
    project_uuid: UUID,
    request: SensorsRequest,
    svc: ProjectService = Depends(get_project_service),
):
    """Update sensor positions for a project."""

    def mutate(robot_config: dict) -> None:
        robot_config.setdefault("physical", {})
        sensors_data = []
        for s in request.sensors:
            sensor_dict = {"name": s.name}
            if s.x_cm is not None:
                sensor_dict["x_cm"] = s.x_cm
            if s.y_cm is not None:
                sensor_dict["y_cm"] = s.y_cm
            if s.clearance_cm is not None:
                sensor_dict["clearance_cm"] = s.clearance_cm
            sensors_data.append(sensor_dict)
        robot_config["physical"]["sensors"] = sensors_data

    updated = _update_robot_config(project_uuid, svc, mutate)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _build_device_info(updated)


@router.put("/{project_uuid}/rotation-center", response_model=DeviceInfo)
async def update_rotation_center(
    project_uuid: UUID,
    request: RotationCenterRequest,
    svc: ProjectService = Depends(get_project_service),
):
    """Update rotation center for a project."""

    def mutate(robot_config: dict) -> None:
        robot_config.setdefault("physical", {})
        if request.rotation_center:
            robot_config["physical"]["rotation_center"] = {
                "x_cm": request.rotation_center.x_cm,
                "y_cm": request.rotation_center.y_cm,
            }
        elif "rotation_center" in robot_config["physical"]:
            del robot_config["physical"]["rotation_center"]

    updated = _update_robot_config(project_uuid, svc, mutate)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _build_device_info(updated)


@router.put("/{project_uuid}/start-pose", response_model=DeviceInfo)
async def update_start_pose(
    project_uuid: UUID,
    request: StartPoseRequest,
    svc: ProjectService = Depends(get_project_service),
):
    """Update start pose for a project."""

    def mutate(robot_config: dict) -> None:
        robot_config.setdefault("physical", {})
        robot_config["physical"]["start_pose"] = {
            "x_cm": request.start_pose.x_cm,
            "y_cm": request.start_pose.y_cm,
            "theta_deg": request.start_pose.theta_deg,
        }

    updated = _update_robot_config(project_uuid, svc, mutate)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _build_device_info(updated)


@router.put("/{project_uuid}/kinematics", response_model=DeviceInfo)
async def update_kinematics(
    project_uuid: UUID,
    request: KinematicsRequest,
    svc: ProjectService = Depends(get_project_service),
):
    """Update kinematics (track width, wheelbase, wheel radius) for a project."""

    def mutate(robot_config: dict) -> None:
        if "drive" not in robot_config:
            robot_config["drive"] = {}
        if "kinematics" not in robot_config["drive"]:
            robot_config["drive"]["kinematics"] = {}

        kinematics = robot_config["drive"]["kinematics"]
        if request.track_width_m is not None:
            kinematics["track_width"] = request.track_width_m
        if request.wheelbase_m is not None:
            kinematics["wheelbase"] = request.wheelbase_m
        if request.wheel_radius_m is not None:
            kinematics["wheel_radius"] = request.wheel_radius_m

    updated = _update_robot_config(project_uuid, svc, mutate)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _build_device_info(updated)


def _table_map_v2_or_422(payload: object) -> dict:
    """Validate a raw table map as v2, raising HTTP 422 on any v1/invalid map."""
    try:
        return parse_v2(payload)
    except TableMapVersionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


@router.get("/{project_uuid}/table-map")
async def get_table_map(
    project_uuid: UUID,
    svc: ProjectService = Depends(get_project_service),
):
    """Get table map for a project (always v2).

    If physical.table_map is a file path, reads and returns the file content.
    If it is inline data, returns it directly. v1 (legacy) maps are rejected
    with 422 — v1 support was dropped, the map must be re-saved as v2.
    """
    import json

    config = svc.project_repository.read_project_config(project_uuid)
    if not config:
        return {"map": None}
    physical = config.get("robot", {}).get("physical", {})
    table_map = physical.get("table_map")

    if isinstance(table_map, str):
        project_path = svc.get_project_path(project_uuid)
        map_file = project_path / table_map
        if not map_file.exists():
            return {"map": None}
        try:
            with open(map_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {"map": None}
        return {"map": _table_map_v2_or_422(raw)}

    if table_map is None:
        return {"map": None}
    return {"map": _table_map_v2_or_422(table_map)}


@router.put("/{project_uuid}/table-map")
async def update_table_map(
    project_uuid: UUID,
    request: TableMapRequest,
    svc: ProjectService = Depends(get_project_service),
):
    """Update table map for a project.

    If physical.table_map is already a file path, writes the map data to that file
    and preserves the path reference in robot.yml. Otherwise saves inline.
    """
    import json

    config = svc.project_repository.read_project_config(project_uuid)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    physical = config.get("robot", {}).get("physical", {})
    existing_map_ref = physical.get("table_map")

    # Always persist in canonical v2 shape (request is v2-only by schema).
    v2_payload = request.to_dict()
    has_content = any(layer.get("lines") for layer in v2_payload["layers"]) or v2_payload["transitions"]

    if isinstance(existing_map_ref, str):
        # Write map data to the referenced file, keep path in robot.yml unchanged.
        project_path = svc.get_project_path(project_uuid)
        map_file = project_path / existing_map_ref
        map_file.parent.mkdir(parents=True, exist_ok=True)
        with open(map_file, "w", encoding="utf-8") as f:
            json.dump(v2_payload, f, indent=2)
        return {"success": True}

    # Inline fallback: save data directly into robot.yml.
    def mutate(robot_config: dict) -> None:
        robot_config.setdefault("physical", {})
        if has_content:
            robot_config["physical"]["table_map"] = v2_payload
        elif "table_map" in robot_config["physical"]:
            del robot_config["physical"]["table_map"]

    updated = _update_robot_config(project_uuid, svc, mutate)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return {"success": True}
