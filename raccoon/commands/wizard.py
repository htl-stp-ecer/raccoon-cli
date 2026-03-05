"""Interactive project setup wizard."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


# Module-level reference to API client for remote calibration
_api_client: Optional["RaccoonApiClient"] = None


def set_api_client(client: "RaccoonApiClient") -> None:
    """Set the API client for remote encoder reading."""
    global _api_client
    _api_client = client


def clear_api_client() -> None:
    """Clear the API client reference."""
    global _api_client
    _api_client = None


def _prompt_measurements(existing_config: Dict[str, object]) -> Dict[str, float]:
    """Collect measurement data from the user, using existing config values as defaults."""
    # Extract existing values from config if available
    robot = existing_config.get("robot", {})
    drive = robot.get("drive", {}) if isinstance(robot, dict) else {}
    kinematics = drive.get("kinematics", {}) if isinstance(drive, dict) else {}
    limits = drive.get("limits", {}) if isinstance(drive, dict) else {}
    definitions = existing_config.get("definitions", {})

    # Calculate defaults from existing config
    existing_wheel_radius = kinematics.get("wheel_radius") if isinstance(kinematics, dict) else None
    default_wheel_diameter_mm = round(existing_wheel_radius * 2 * 1000, 1) if existing_wheel_radius else 75.0

    existing_track_width = kinematics.get("track_width") if isinstance(kinematics, dict) else None
    default_track_width_cm = round(existing_track_width * 100, 1) if existing_track_width else 20.0

    existing_wheelbase = kinematics.get("wheelbase") if isinstance(kinematics, dict) else None
    default_wheelbase_cm = round(existing_wheelbase * 100, 1) if existing_wheelbase else 15.0

    # Try to get vel_lpf_alpha from any motor definition
    default_vel_filter_alpha = 0.8
    if isinstance(definitions, dict):
        for defn in definitions.values():
            if isinstance(defn, dict) and defn.get("type") == "Motor":
                calib = defn.get("calibration", {})
                if isinstance(calib, dict) and "vel_lpf_alpha" in calib:
                    default_vel_filter_alpha = calib["vel_lpf_alpha"]
                    break

    wheel_diameter_mm = click.prompt("Wheel diameter (mm)", default=default_wheel_diameter_mm, type=float)
    track_width_cm = click.prompt("Track width (cm, left ↔ right wheel centers)", default=default_track_width_cm, type=float)
    wheelbase_cm = click.prompt("Wheelbase (cm, front ↔ rear axle centers)", default=default_wheelbase_cm, type=float)
    vel_filter_alpha = click.prompt("Velocity low-pass alpha (0-1)", default=default_vel_filter_alpha, type=float)

    return {
        "wheel_diameter_mm": wheel_diameter_mm,
        "track_width_cm": track_width_cm,
        "wheelbase_cm": wheelbase_cm,
        "vel_filter_alpha": vel_filter_alpha,
    }


def _prompt_drive_type(existing: str | None) -> str:
    """Ask for drivetrain selection."""
    choice = click.prompt(
        "Drivetrain type",
        default=existing or "mecanum",
        type=click.Choice(["mecanum", "differential"], case_sensitive=False),
    )
    return choice.lower()


def _collect_motor_data(drivetrain: str, existing_config: Dict[str, object]) -> Dict[str, Tuple[int, bool]]:
    """
    Collect motor connection details.

    Returns a dict mapping definition keys to (port, inverted) pairs.
    Uses existing definitions as defaults if available.
    """
    definitions = existing_config.get("definitions", {})
    if not isinstance(definitions, dict):
        definitions = {}

    if drivetrain == "mecanum":
        order = [
            ("front_left_motor", "Front-left"),
            ("front_right_motor", "Front-right"),
            ("rear_left_motor", "Rear-left"),
            ("rear_right_motor", "Rear-right"),
        ]
    else:
        order = [
            ("left_motor", "Left"),
            ("right_motor", "Right"),
        ]

    motors: Dict[str, Tuple[int, bool]] = {}
    default_port = 0
    for key, label in order:
        # Get defaults from existing definition if available
        existing_def = definitions.get(key, {})
        if isinstance(existing_def, dict) and existing_def.get("type") == "Motor":
            existing_port = existing_def.get("port", default_port)
            existing_inverted = existing_def.get("inverted", key.endswith("right_motor"))
        else:
            existing_port = default_port
            existing_inverted = key.endswith("right_motor")

        port = click.prompt(f"{label} motor port", type=int, default=existing_port)
        inverted = click.confirm(f"Is the {label.lower()} motor inverted?", default=existing_inverted)
        motors[key] = (port, inverted)
        default_port += 1

    return motors


def _build_motor_definition(port: int, inverted: bool, ticks_to_rad: float, vel_lpf_alpha: float) -> Dict[str, object]:
    """Return a baseline motor definition."""
    return {
        "type": "Motor",
        "port": port,
        "inverted": inverted,
        "calibration": {
            "ff": {"kS": 0.08, "kV": 0.12, "kA": 0.1},
            "pid": {"kp": 2.4, "ki": 0.3, "kd": 0.08},
            "ticks_to_rad": round(ticks_to_rad, 7),
            "vel_lpf_alpha": round(vel_lpf_alpha, 3),
        },
    }


def _create_definitions(
    motors: Dict[str, Tuple[int, bool]],
    ticks_to_rad: Dict[str, float],
    vel_lpf_alpha: float,
) -> Dict[str, object]:
    """Create the definitions section."""
    definitions = {
        name: _build_motor_definition(port, inverted, ticks_to_rad[name], vel_lpf_alpha)
        for name, (port, inverted) in motors.items()
    }
    definitions.setdefault("imu", {"type": "IMU"})
    return definitions


def _build_kinematics_config(
    drivetrain: str,
    motors: Dict[str, Tuple[int, bool]],
    measures: Dict[str, float],
) -> Dict[str, object]:
    """Create a kinematics configuration payload."""
    wheel_radius = (measures["wheel_diameter_mm"] / 1000.0) / 2.0
    track_width = measures["track_width_cm"] / 100.0
    wheelbase = measures["wheelbase_cm"] / 100.0

    config: Dict[str, object] = {
        "type": drivetrain,
        "wheel_radius": round(wheel_radius, 5),
        "track_width": round(track_width, 4),
    }

    if drivetrain == "mecanum":
        config["wheelbase"] = round(wheelbase, 4)
        config.update(
            {
                "front_left_motor": "front_left_motor",
                "front_right_motor": "front_right_motor",
                "back_left_motor": "rear_left_motor",
                "back_right_motor": "rear_right_motor",
            }
        )
    else:
        config.update(
            {
                "left_motor": "left_motor",
                "right_motor": "right_motor",
            }
        )

    return config


def _build_robot_config(
    drivetrain: str,
    motors: Dict[str, Tuple[int, bool]],
    measures: Dict[str, float],
) -> Dict[str, object]:
    """Assemble the robot configuration."""
    kinematics = _build_kinematics_config(drivetrain, motors, measures)

    odometry_defaults = {
        "type": "FusedOdometry",
        "invert_x": False,
        "invert_y": False,
        "invert_z": True,
        "invert_w": False,
    }

    return {
        "drive": {
            "kinematics": kinematics,
        },
        "odometry": odometry_defaults,
    }


def _render_summary(console: Console, config: Dict[str, object]) -> None:
    """Pretty-print the resulting configuration summary."""
    robot = config.get("robot", {})
    definitions = config.get("definitions", {})

    table = Table(title="Wizard Summary", expand=True)
    table.add_column("Section", style="bold cyan")
    table.add_column("Details")

    table.add_row("Project", f"name: {config.get('name', 'Unnamed Project')}\nuuid: {config.get('uuid', '—')}")
    table.add_row("Drive", yaml.safe_dump(robot, sort_keys=False))
    table.add_row("Definitions", yaml.safe_dump(definitions, sort_keys=False))

    console.print(Panel(table, border_style="green"))


def _read_encoder_position_remote(port: int, inverted: bool) -> int:
    """Read encoder position from remote Pi via API."""
    global _api_client
    if _api_client is None:
        raise RuntimeError("No API client configured for remote encoder reading")

    async def _read():
        async with _api_client:
            reading = await _api_client.read_encoder(port, inverted)
            if not reading.success:
                raise RuntimeError(f"Failed to read encoder: {reading.error}")
            return reading.position

    return asyncio.run(_read())


def _calibrate_single_wheel_remote(
    console: Console, port: int, inverted: bool, motor_name: str, num_trials: int = 3
) -> float:
    """
    Calibrate a single wheel by having the user rotate it manually (remote mode).

    Records the encoder position before and after one full rotation via API,
    averaging across multiple trials.
    """
    measurements = []

    for trial in range(1, num_trials + 1):
        console.print(f"\n[bold cyan]Trial {trial}/{num_trials} for {motor_name}[/bold cyan]")

        # Record starting position via remote API
        start_pos = _read_encoder_position_remote(port, inverted)
        console.print(f"[dim]Starting encoder position: {start_pos}[/dim]")

        console.print(
            "[green]→ Mark the wheel position, then rotate it exactly ONE full turn "
            "(360°) by hand.[/green]"
        )
        click.prompt("Press Enter when you have completed the rotation", default="", show_default=False)

        # Record ending position via remote API
        end_pos = _read_encoder_position_remote(port, inverted)
        ticks = abs(end_pos - start_pos)

        console.print(f"[dim]Ending encoder position: {end_pos}[/dim]")
        console.print(f"[cyan]Measured: {ticks} ticks for this rotation[/cyan]")

        measurements.append(ticks)

    avg_ticks = sum(measurements) / len(measurements)
    console.print(f"\n[bold green]{motor_name} average: {avg_ticks:.1f} ticks/revolution[/bold green]")
    console.print(f"[dim]Individual measurements: {measurements}[/dim]")

    return avg_ticks


def _calibrate_single_wheel_local(
    console: Console, motor, motor_name: str, num_trials: int = 3
) -> float:
    """
    Calibrate a single wheel by having the user rotate it manually (local mode).

    Records the encoder position before and after one full rotation,
    averaging across multiple trials.
    """
    measurements = []

    for trial in range(1, num_trials + 1):
        console.print(f"\n[bold cyan]Trial {trial}/{num_trials} for {motor_name}[/bold cyan]")

        # Record starting position
        start_pos = motor.get_position()
        console.print(f"[dim]Starting encoder position: {start_pos}[/dim]")

        console.print(
            "[green]→ Mark the wheel position, then rotate it exactly ONE full turn "
            "(360°) by hand.[/green]"
        )
        click.prompt("Press Enter when you have completed the rotation", default="", show_default=False)

        # Record ending position
        end_pos = motor.get_position()
        ticks = abs(end_pos - start_pos)

        console.print(f"[dim]Ending encoder position: {end_pos}[/dim]")
        console.print(f"[cyan]Measured: {ticks} ticks for this rotation[/cyan]")

        measurements.append(ticks)

    avg_ticks = sum(measurements) / len(measurements)
    console.print(f"\n[bold green]{motor_name} average: {avg_ticks:.1f} ticks/revolution[/bold green]")
    console.print(f"[dim]Individual measurements: {measurements}[/dim]")

    return avg_ticks


def _print_calibration_summary(console: Console, results: Dict[str, int]) -> None:
    """Print a summary table of calibration results per motor."""
    console.print("\n[bold green]Calibration Results:[/bold green]")
    table = Table()
    table.add_column("Motor", style="cyan")
    table.add_column("Ticks/Rev", justify="right")
    table.add_column("Rad/Tick", justify="right")

    for motor_name, ticks in results.items():
        rad_per_tick = (2 * math.pi) / ticks
        table.add_row(motor_name, str(ticks), f"{rad_per_tick:.7f}")

    console.print(table)


def _calibrate_ticks_per_rev(
    console: Console, motor_defs: Dict[str, Tuple[int, bool]]
) -> Dict[str, int]:
    """
    Interactive helper to measure encoder ticks per wheel revolution.

    For each wheel, the user manually rotates the wheel one full turn
    while the wizard measures the encoder tick difference. Each wheel
    is measured 3 times and averaged individually.

    Uses remote API if an API client is configured, otherwise tries local hardware.

    Returns:
        Dict mapping motor name to its calibrated ticks per revolution.
    """
    global _api_client
    num_trials = 3
    available_motors = list(motor_defs.keys())
    results: Dict[str, int] = {}

    # Try remote calibration first if API client is configured
    if _api_client is not None:
        console.print(
            "\n[bold cyan]Encoder Calibration (Remote)[/bold cyan]\n"
            f"For each wheel, you will rotate it exactly ONE full turn {num_trials} times.\n"
            "The wizard will record the encoder ticks via the Pi and average the results.\n"
        )

        for motor_name in available_motors:
            port, inverted = motor_defs[motor_name]

            console.print(f"\n[bold]Calibrating: {motor_name} (port {port})[/bold]")
            console.print("[yellow]Make sure the wheel can spin freely.[/yellow]")

            if not click.confirm(f"Ready to calibrate {motor_name}?", default=True):
                console.print(f"[yellow]Skipping {motor_name} - using default[/yellow]")
                results[motor_name] = 1536
                continue

            try:
                avg = _calibrate_single_wheel_remote(console, port, inverted, motor_name, num_trials)
                results[motor_name] = int(round(avg))
            except Exception as exc:
                console.print(f"[red]Error calibrating {motor_name}: {exc}[/red]")
                results[motor_name] = 1536

        _print_calibration_summary(console, results)
        return results

    # Fall back to local calibration
    try:
        from libstp.hal import Motor as HalMotor  # type: ignore

        console.print(
            "\n[bold cyan]Encoder Calibration (Local)[/bold cyan]\n"
            f"For each wheel, you will rotate it exactly ONE full turn {num_trials} times.\n"
            "The wizard will record the encoder ticks and average the results.\n"
        )

        for motor_name in available_motors:
            port, inverted = motor_defs[motor_name]
            motor = HalMotor(port=port, inverted=inverted)

            console.print(f"\n[bold]Calibrating: {motor_name} (port {port})[/bold]")
            console.print("[yellow]Make sure the wheel can spin freely.[/yellow]")

            if not click.confirm(f"Ready to calibrate {motor_name}?", default=True):
                console.print(f"[yellow]Skipping {motor_name} - using default[/yellow]")
                results[motor_name] = 1536
                continue

            avg = _calibrate_single_wheel_local(console, motor, motor_name, num_trials)
            results[motor_name] = int(round(avg))

        _print_calibration_summary(console, results)
        return results

    except Exception as exc:  # pylint: disable=broad-except
        console.print(
            f"[yellow]Could not access motor hardware ({exc}).[/yellow]\n"
            "[cyan]Falling back to manual entry.[/cyan]"
        )
        default_ticks = click.prompt("Encoder ticks per wheel revolution", default=1536, type=int)
        return {name: default_ticks for name in available_motors}


def _ensure_remote_connection(console: Console, project_root: Path) -> bool:
    """
    Ensure a connection to the Pi is established for remote calibration.

    Returns True if connected (and API client is set), False otherwise.
    """
    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client

    manager = get_connection_manager()

    # Try to auto-connect from project or global config if not connected
    if not manager.is_connected:
        # Try project config first
        project_conn = manager.load_from_project(project_root)
        if project_conn and project_conn.pi_address:
            console.print(f"[cyan]Connecting to Pi at {project_conn.pi_address}...[/cyan]")
            try:
                manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
            except Exception as e:
                console.print(f"[yellow]Failed to connect: {e}[/yellow]")
                return False
        else:
            # Try global config
            known_pis = manager.load_known_pis()
            if known_pis:
                pi = known_pis[0]
                console.print(f"[cyan]Connecting to Pi at {pi.get('address')}...[/cyan]")
                try:
                    manager.connect_sync(pi.get("address"), pi.get("port", 8421))
                except Exception as e:
                    console.print(f"[yellow]Failed to connect: {e}[/yellow]")
                    return False
            else:
                return False

    if not manager.is_connected:
        return False

    # Create and set the API client
    state = manager.state
    client = create_api_client(state.pi_address, state.pi_port, api_token=state.api_token)
    set_api_client(client)

    console.print(f"[green]Connected to {state.pi_hostname}[/green]")
    return True


def _prompt_ticks_per_rev(
    console: Console, motor_defs: Dict[str, Tuple[int, bool]], existing_config: Dict[str, object], project_root: Path
) -> Dict[str, int]:
    """
    Ask whether to calibrate encoder ticks interactively or accept a prompt.

    Returns:
        Dict mapping motor name to its ticks per revolution.
    """
    # Calculate defaults from existing ticks_to_rad values per motor
    definitions = existing_config.get("definitions", {})
    default_ticks: Dict[str, int] = {}

    if isinstance(definitions, dict):
        for motor_name in motor_defs:
            defn = definitions.get(motor_name, {})
            if isinstance(defn, dict) and defn.get("type") == "Motor":
                calib = defn.get("calibration", {})
                if isinstance(calib, dict) and "ticks_to_rad" in calib:
                    ticks_to_rad = calib["ticks_to_rad"]
                    if ticks_to_rad > 0:
                        default_ticks[motor_name] = int(round((2 * math.pi) / ticks_to_rad))

    # Fill in missing defaults
    for motor_name in motor_defs:
        if motor_name not in default_ticks:
            default_ticks[motor_name] = 1536

    if not click.confirm("Run the guided encoder tick calibration?", default=False):
        # Use same value for all motors when manually entering
        single_default = next(iter(default_ticks.values()), 1536)
        ticks = click.prompt("Encoder ticks per wheel revolution", default=single_default, type=int)
        return {name: ticks for name in motor_defs}

    # Before running calibration, ensure we have a remote connection
    # (calibration needs to run on the Pi with actual hardware)
    if not _ensure_remote_connection(console, project_root):
        console.print("[yellow]No Pi connection available.[/yellow]")
        console.print("[cyan]To run encoder calibration, connect to the Pi first with 'raccoon connect <pi-address>'[/cyan]")
        console.print("[cyan]Falling back to manual entry.[/cyan]")
        single_default = next(iter(default_ticks.values()), 1536)
        ticks = click.prompt("Encoder ticks per wheel revolution", default=single_default, type=int)
        return {name: ticks for name in motor_defs}

    try:
        return _calibrate_ticks_per_rev(console, motor_defs)
    finally:
        # Clean up the API client
        clear_api_client()


@click.command(name="wizard")
@click.option("--dry-run", is_flag=True, help="Preview output without writing raccoon.project.yml")
@click.pass_context
def wizard_command(ctx: click.Context, dry_run: bool) -> None:
    """Launch an interactive wizard to scaffold raccoon.project.yml."""
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    try:
        existing_config = load_project_config(project_root)
    except ProjectError:
        existing_config = {}

    project_name = click.prompt(
        "Project name",
        default=existing_config.get("name", "My Raccoon Robot"),
    )

    drivetrain = _prompt_drive_type(existing_config.get("robot", {}).get("drive", {}).get("kinematics", {}).get("type"))
    motor_defs = _collect_motor_data(drivetrain, existing_config)
    measurements = _prompt_measurements(existing_config)
    ticks_per_rev = _prompt_ticks_per_rev(console, motor_defs, existing_config, project_root)

    # Convert ticks per revolution to radians per tick for each motor
    ticks_to_rad = {name: (2 * math.pi) / ticks for name, ticks in ticks_per_rev.items()}

    config: Dict[str, object] = dict(existing_config)
    config["name"] = project_name
    config.setdefault("uuid", existing_config.get("uuid", ""))
    config["robot"] = _build_robot_config(drivetrain, motor_defs, measurements)

    definitions = existing_config.get("definitions", {}).copy()
    definitions.update(_create_definitions(motor_defs, ticks_to_rad, measurements["vel_filter_alpha"]))
    config["definitions"] = definitions

    _render_summary(console, config)

    if dry_run:
        console.print("[yellow]Dry run enabled — raccoon.project.yml was not updated.[/yellow]")
        return

    if not click.confirm("Write these values to raccoon.project.yml?", default=True):
        console.print("[yellow]Aborted without writing changes.[/yellow]")
        return

    config_path = project_root / "raccoon.project.yml"
    from raccoon.yaml_utils import save_yaml

    save_yaml(config, config_path)

    console.print(f"[green]Updated {config_path.relative_to(project_root)} with wizard output.[/green]")
