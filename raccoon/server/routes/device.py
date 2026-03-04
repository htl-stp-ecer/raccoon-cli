"""Device configuration endpoints for physical robot settings."""

import socket
import subprocess
from pathlib import Path
from typing import List, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon.server.auth import require_auth

router = APIRouter(prefix="/api/v1/device", tags=["device"], dependencies=[Depends(require_auth)])


# =============================================================================
# Pydantic Models
# =============================================================================


class SensorInfo(BaseModel):
    """Sensor position information."""

    name: str
    x_pct: Optional[float] = None
    y_pct: Optional[float] = None
    clearance_cm: Optional[float] = None


class CenterPoint(BaseModel):
    """Center point position as percentages."""

    x_pct: float
    y_pct: float


class StartPose(BaseModel):
    """Starting pose on the table."""

    x_cm: float
    y_cm: float
    theta_deg: float


class ConnectionInfo(BaseModel):
    """Complete device info returned to clients."""

    hostname: str
    ip: str
    battery_voltage_v: Optional[float] = None
    battery_percent: Optional[float] = None
    width_cm: Optional[float] = None
    length_cm: Optional[float] = None
    sensors: Optional[List[SensorInfo]] = None
    rotation_center: Optional[CenterPoint] = None
    start_pose: Optional[StartPose] = None
    # Kinematics info (read-only, from robot.drive.kinematics)
    drive_type: Optional[str] = None
    track_width_m: Optional[float] = None
    wheelbase_m: Optional[float] = None


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


class TableMapRequest(BaseModel):
    """Request to set table map image."""

    image: str  # Base64-encoded image


class HostnameRequest(BaseModel):
    """Request to change hostname."""

    hostname: str


# =============================================================================
# Helper Functions
# =============================================================================


def _get_project_path() -> Optional[Path]:
    """Get the current project path from the projects directory."""
    from raccoon.server.app import get_config

    config = get_config()
    projects_dir = config.projects_dir

    if not projects_dir.exists():
        return None

    # Find the first project with a raccoon.project.yml
    for item in projects_dir.iterdir():
        if item.is_dir():
            config_path = item / "raccoon.project.yml"
            if config_path.exists():
                return item

    return None


def _load_project_config(project_path: Path) -> dict:
    """Load the project YAML configuration."""
    config_path = project_path / "raccoon.project.yml"
    if not config_path.exists():
        return {}

    from raccoon.yaml_utils import load_yaml

    return load_yaml(config_path)


def _save_project_config(project_path: Path, config: dict) -> None:
    """Save the project YAML configuration."""
    from raccoon.yaml_utils import save_yaml

    config_path = project_path / "raccoon.project.yml"
    save_yaml(config, config_path)


def _get_hostname() -> str:
    """Get the system hostname."""
    return socket.gethostname()


def _get_ip() -> str:
    """Get the primary IP address."""
    try:
        # Create a UDP socket to determine primary interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_battery_info() -> tuple[Optional[float], Optional[float]]:
    """Get battery voltage and percentage from the Wombat HAL."""
    try:
        from libstp.hal import Wombat

        wombat = Wombat()
        voltage = wombat.battery_voltage()
        # Estimate percentage (typical LiPo range: 11.1V min, 12.6V max for 3S)
        min_v, max_v = 11.1, 12.6
        percent = max(0.0, min(100.0, (voltage - min_v) / (max_v - min_v) * 100.0))
        return voltage, percent
    except Exception:
        return None, None


def _build_connection_info(project_path: Optional[Path]) -> ConnectionInfo:
    """Build a ConnectionInfo object from system and project data."""
    hostname = _get_hostname()
    ip = _get_ip()
    battery_v, battery_pct = _get_battery_info()

    # Physical config defaults
    width_cm: Optional[float] = None
    length_cm: Optional[float] = None
    sensors: Optional[List[SensorInfo]] = None
    rotation_center: Optional[CenterPoint] = None
    start_pose: Optional[StartPose] = None
    drive_type: Optional[str] = None
    track_width_m: Optional[float] = None
    wheelbase_m: Optional[float] = None

    if project_path:
        config = _load_project_config(project_path)
        robot_config = config.get("robot", {})
        physical = robot_config.get("physical", {})

        width_cm = physical.get("width_cm")
        length_cm = physical.get("length_cm")

        # Sensors
        sensor_list = physical.get("sensors", [])
        if sensor_list:
            sensors = [
                SensorInfo(
                    name=s.get("name", ""),
                    x_pct=s.get("x_pct"),
                    y_pct=s.get("y_pct"),
                    clearance_cm=s.get("clearance_cm"),
                )
                for s in sensor_list
            ]

        # Rotation center
        rc = physical.get("rotation_center")
        if rc:
            rotation_center = CenterPoint(x_pct=rc.get("x_pct", 50), y_pct=rc.get("y_pct", 50))

        # Start pose
        sp = physical.get("start_pose")
        if sp:
            start_pose = StartPose(
                x_cm=sp.get("x_cm", 0),
                y_cm=sp.get("y_cm", 0),
                theta_deg=sp.get("theta_deg", 0),
            )

        # Kinematics (read-only)
        drive_config = robot_config.get("drive", {})
        kinematics = drive_config.get("kinematics", {})
        drive_type = kinematics.get("type")
        track_width_m = kinematics.get("track_width")
        wheelbase_m = kinematics.get("wheelbase")

    return ConnectionInfo(
        hostname=hostname,
        ip=ip,
        battery_voltage_v=battery_v,
        battery_percent=battery_pct,
        width_cm=width_cm,
        length_cm=length_cm,
        sensors=sensors,
        rotation_center=rotation_center,
        start_pose=start_pose,
        drive_type=drive_type,
        track_width_m=track_width_m,
        wheelbase_m=wheelbase_m,
    )


def _ensure_physical_section(config: dict) -> dict:
    """Ensure the robot.physical section exists in config."""
    if "robot" not in config:
        config["robot"] = {}
    if "physical" not in config["robot"]:
        config["robot"]["physical"] = {}
    return config


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/info", response_model=ConnectionInfo)
async def get_device_info():
    """Get device information including physical configuration."""
    project_path = _get_project_path()
    return _build_connection_info(project_path)


@router.put("/hostname", response_model=ConnectionInfo)
async def update_hostname(request: HostnameRequest):
    """Update the system hostname."""
    new_hostname = request.hostname.strip()
    if not new_hostname:
        raise HTTPException(status_code=400, detail="Hostname cannot be empty")

    try:
        # Update hostname using hostnamectl
        subprocess.run(["sudo", "hostnamectl", "set-hostname", new_hostname], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Failed to set hostname: {e.stderr.decode()}") from e
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="hostnamectl not available")

    project_path = _get_project_path()
    return _build_connection_info(project_path)


@router.put("/dimensions", response_model=ConnectionInfo)
async def update_dimensions(request: DimensionsRequest):
    """Update robot dimensions."""
    if request.width_cm <= 0 or request.length_cm <= 0:
        raise HTTPException(status_code=400, detail="Dimensions must be positive")

    project_path = _get_project_path()
    if not project_path:
        raise HTTPException(status_code=404, detail="No project found")

    config = _load_project_config(project_path)
    config = _ensure_physical_section(config)

    config["robot"]["physical"]["width_cm"] = request.width_cm
    config["robot"]["physical"]["length_cm"] = request.length_cm

    _save_project_config(project_path, config)
    return _build_connection_info(project_path)


@router.put("/sensors", response_model=ConnectionInfo)
async def update_sensors(request: SensorsRequest):
    """Update sensor positions."""
    project_path = _get_project_path()
    if not project_path:
        raise HTTPException(status_code=404, detail="No project found")

    config = _load_project_config(project_path)
    config = _ensure_physical_section(config)

    # Convert to list of dicts for YAML
    sensors_data = []
    for s in request.sensors:
        sensor_dict = {"name": s.name}
        if s.x_pct is not None:
            sensor_dict["x_pct"] = s.x_pct
        if s.y_pct is not None:
            sensor_dict["y_pct"] = s.y_pct
        if s.clearance_cm is not None:
            sensor_dict["clearance_cm"] = s.clearance_cm
        sensors_data.append(sensor_dict)

    config["robot"]["physical"]["sensors"] = sensors_data

    _save_project_config(project_path, config)
    return _build_connection_info(project_path)


@router.put("/rotation-center", response_model=ConnectionInfo)
async def update_rotation_center(request: RotationCenterRequest):
    """Update rotation center position."""
    project_path = _get_project_path()
    if not project_path:
        raise HTTPException(status_code=404, detail="No project found")

    config = _load_project_config(project_path)
    config = _ensure_physical_section(config)

    if request.rotation_center:
        config["robot"]["physical"]["rotation_center"] = {
            "x_pct": request.rotation_center.x_pct,
            "y_pct": request.rotation_center.y_pct,
        }
    elif "rotation_center" in config["robot"]["physical"]:
        del config["robot"]["physical"]["rotation_center"]

    _save_project_config(project_path, config)
    return _build_connection_info(project_path)


@router.put("/start-pose", response_model=ConnectionInfo)
async def update_start_pose(request: StartPoseRequest):
    """Update starting pose on the table."""
    project_path = _get_project_path()
    if not project_path:
        raise HTTPException(status_code=404, detail="No project found")

    config = _load_project_config(project_path)
    config = _ensure_physical_section(config)

    config["robot"]["physical"]["start_pose"] = {
        "x_cm": request.start_pose.x_cm,
        "y_cm": request.start_pose.y_cm,
        "theta_deg": request.start_pose.theta_deg,
    }

    _save_project_config(project_path, config)
    return _build_connection_info(project_path)


@router.get("/table-map")
async def get_table_map():
    """Get the table map image."""
    project_path = _get_project_path()
    if not project_path:
        return {"image": None}

    config = _load_project_config(project_path)
    physical = config.get("robot", {}).get("physical", {})
    return {"image": physical.get("table_map")}


@router.put("/table-map")
async def update_table_map(request: TableMapRequest):
    """Update the table map image."""
    project_path = _get_project_path()
    if not project_path:
        raise HTTPException(status_code=404, detail="No project found")

    config = _load_project_config(project_path)
    config = _ensure_physical_section(config)

    if request.image:
        config["robot"]["physical"]["table_map"] = request.image
    elif "table_map" in config["robot"]["physical"]:
        del config["robot"]["physical"]["table_map"]

    _save_project_config(project_path, config)
    return {"success": True}
