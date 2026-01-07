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


def _prompt_measurements() -> Dict[str, float]:
    """Collect measurement data from the user."""
    wheel_diameter_mm = click.prompt("Wheel diameter (mm)", default=75.0, type=float)
    track_width_cm = click.prompt("Track width (cm, left ↔ right wheel centers)", default=20.0, type=float)
    wheelbase_cm = click.prompt("Wheelbase (cm, front ↔ rear axle centers)", default=15.0, type=float)
    desired_max_v = click.prompt("Desired max chassis speed (m/s)", default=1.5, type=float)
    accel_linear = click.prompt(
        "Desired linear acceleration limit (m/s²)", default=round(desired_max_v * 0.8, 2), type=float
    )
    vel_filter_alpha = click.prompt("Velocity low-pass alpha (0-1)", default=0.8, type=float)

    return {
        "wheel_diameter_mm": wheel_diameter_mm,
        "track_width_cm": track_width_cm,
        "wheelbase_cm": wheelbase_cm,
        "max_v": desired_max_v,
        "accel_linear": accel_linear,
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


def _collect_motor_data(drivetrain: str) -> Dict[str, Tuple[int, bool]]:
    """
    Collect motor connection details.

    Returns a dict mapping definition keys to (port, inverted) pairs.
    """
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
        port = click.prompt(f"{label} motor port", type=int, default=default_port)
        inverted = click.confirm(f"Is the {label.lower()} motor inverted?", default=key.endswith("right_motor"))
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
    ticks_to_rad: float,
    vel_lpf_alpha: float,
) -> Dict[str, object]:
    """Create the definitions section."""
    definitions = {
        name: _build_motor_definition(port, inverted, ticks_to_rad, vel_lpf_alpha)
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
    max_v = measures["max_v"]
    max_wheel_velocity = max_v / wheel_radius if wheel_radius > 0 else max_v
    accel_linear = measures["accel_linear"]
    max_wheel_accel = accel_linear / wheel_radius if wheel_radius > 0 else accel_linear

    config: Dict[str, object] = {
        "type": drivetrain,
        "wheel_radius": round(wheel_radius, 5),
        "track_width": round(track_width, 4),
        "max_velocity": round(max_wheel_velocity, 3),
        "max_acceleration": round(max_wheel_accel, 3),
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
    track_width = measures["track_width_cm"] / 100.0
    max_v = measures["max_v"]
    max_omega = max_v / (track_width / 2.0) if track_width > 0 else max_v

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
            "limits": {
                "max_v": round(max_v, 3),
                "max_omega": round(max_omega, 3),
            },
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


def _calibrate_ticks_per_rev(console: Console, motor_defs: Dict[str, Tuple[int, bool]]) -> int:
    """
    Interactive helper to measure encoder ticks per wheel revolution.

    For each wheel, the user manually rotates the wheel one full turn
    while the wizard measures the encoder tick difference. Each wheel
    is measured 3 times and averaged.

    Uses remote API if an API client is configured, otherwise tries local hardware.
    """
    global _api_client
    num_trials = 3
    available_motors = list(motor_defs.keys())

    # Try remote calibration first if API client is configured
    if _api_client is not None:
        console.print(
            "\n[bold cyan]Encoder Calibration (Remote)[/bold cyan]\n"
            f"For each wheel, you will rotate it exactly ONE full turn {num_trials} times.\n"
            "The wizard will record the encoder ticks via the Pi and average the results.\n"
        )

        all_averages = []

        for motor_name in available_motors:
            port, inverted = motor_defs[motor_name]

            console.print(f"\n[bold]Calibrating: {motor_name} (port {port})[/bold]")
            console.print("[yellow]Make sure the wheel can spin freely.[/yellow]")

            if not click.confirm(f"Ready to calibrate {motor_name}?", default=True):
                console.print(f"[yellow]Skipping {motor_name}[/yellow]")
                continue

            try:
                avg = _calibrate_single_wheel_remote(console, port, inverted, motor_name, num_trials)
                all_averages.append(avg)
            except Exception as exc:
                console.print(f"[red]Error calibrating {motor_name}: {exc}[/red]")
                continue

        if not all_averages:
            console.print("[yellow]No wheels calibrated. Using default value.[/yellow]")
            return 1536

        # Average across all calibrated wheels
        final_avg = sum(all_averages) / len(all_averages)
        console.print(f"\n[bold green]Final average across all wheels: {final_avg:.1f} ticks/revolution[/bold green]")

        return int(round(final_avg))

    # Fall back to local calibration
    try:
        from libstp.hal import Motor as HalMotor  # type: ignore

        console.print(
            "\n[bold cyan]Encoder Calibration (Local)[/bold cyan]\n"
            f"For each wheel, you will rotate it exactly ONE full turn {num_trials} times.\n"
            "The wizard will record the encoder ticks and average the results.\n"
        )

        all_averages = []

        for motor_name in available_motors:
            port, inverted = motor_defs[motor_name]
            motor = HalMotor(port=port, inverted=inverted)

            console.print(f"\n[bold]Calibrating: {motor_name} (port {port})[/bold]")
            console.print("[yellow]Make sure the wheel can spin freely.[/yellow]")

            if not click.confirm(f"Ready to calibrate {motor_name}?", default=True):
                console.print(f"[yellow]Skipping {motor_name}[/yellow]")
                continue

            avg = _calibrate_single_wheel_local(console, motor, motor_name, num_trials)
            all_averages.append(avg)

        if not all_averages:
            console.print("[yellow]No wheels calibrated. Using default value.[/yellow]")
            return 1536

        # Average across all calibrated wheels
        final_avg = sum(all_averages) / len(all_averages)
        console.print(f"\n[bold green]Final average across all wheels: {final_avg:.1f} ticks/revolution[/bold green]")

        return int(round(final_avg))

    except Exception as exc:  # pylint: disable=broad-except
        console.print(
            f"[yellow]Could not access motor hardware ({exc}).[/yellow]\n"
            "[cyan]Falling back to manual entry.[/cyan]"
        )
        return click.prompt("Encoder ticks per wheel revolution", default=1536, type=int)


def _prompt_ticks_per_rev(console: Console, motor_defs: Dict[str, Tuple[int, bool]]) -> int:
    """Ask whether to calibrate encoder ticks interactively or accept a prompt."""
    if not click.confirm("Run the guided encoder tick calibration?", default=False):
        return click.prompt("Encoder ticks per wheel revolution", default=1536, type=int)
    return _calibrate_ticks_per_rev(console, motor_defs)


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
    motor_defs = _collect_motor_data(drivetrain)
    measurements = _prompt_measurements()
    measurements["ticks_per_rev"] = _prompt_ticks_per_rev(console, motor_defs)

    ticks_to_rad = (2 * math.pi) / measurements["ticks_per_rev"]

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
    with open(config_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False)

    console.print(f"[green]Updated {config_path.relative_to(project_root)} with wizard output.[/green]")
