"""Calibration commands for motors and sensors."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from raccoon.project import ProjectError, load_project_config, require_project

from .motors import calibrate_motors_local, calibrate_motors_remote
from .rpm import calibrate_rpm_local, calibrate_rpm_remote
from .benchmark import benchmark_motors_local, benchmark_motors_remote

logger = logging.getLogger("raccoon")


def _require_project_context(console: Console) -> tuple[Path, dict]:
    """Require project context, fail hard if not in a project."""
    try:
        project_root = require_project()
    except ProjectError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        console.print("[yellow]This command must be run from within a raccoon project.[/yellow]")
        raise SystemExit(1) from exc

    try:
        config = load_project_config(project_root)
    except ProjectError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise SystemExit(1) from exc

    return project_root, config


def _require_remote_connection(console: Console, project_root: Path) -> None:
    """Ensure remote connection is available, fail hard if not."""
    from raccoon.client.connection import (
        get_connection_manager,
        VersionMismatchError,
        print_version_mismatch_error,
        ParamikoVersionError,
        print_paramiko_version_error,
    )

    manager = get_connection_manager()

    # Try to auto-connect from project or global config if not connected
    if not manager.is_connected:
        # Try project config first
        project_conn = manager.load_from_project(project_root)
        if project_conn and project_conn.pi_address:
            logger.info(f"Connecting to Pi from project config: {project_conn.pi_address}")
            try:
                manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
            except ParamikoVersionError as e:
                print_paramiko_version_error(e, console)
                raise SystemExit(1)
            except VersionMismatchError as e:
                print_version_mismatch_error(e, console)
                raise SystemExit(1)
            except Exception as e:
                console.print(f"[red]Failed to connect to Pi at {project_conn.pi_address}: {e}[/red]")
                console.print("[yellow]Use --local to run on this machine instead.[/yellow]")
                raise SystemExit(1)
        else:
            # Try global config
            known_pis = manager.load_known_pis()
            if known_pis:
                pi = known_pis[0]
                logger.info(f"Connecting to known Pi: {pi.get('address')}")
                try:
                    manager.connect_sync(pi.get("address"), pi.get("port", 8421))
                except ParamikoVersionError as e:
                    print_paramiko_version_error(e, console)
                    raise SystemExit(1)
                except VersionMismatchError as e:
                    print_version_mismatch_error(e, console)
                    raise SystemExit(1)
                except Exception as e:
                    console.print(f"[red]Failed to connect to Pi at {pi.get('address')}: {e}[/red]")
                    console.print("[yellow]Use --local to run on this machine instead.[/yellow]")
                    raise SystemExit(1)
            else:
                console.print("[red]No Pi connection configured.[/red]")
                console.print("[yellow]Either:[/yellow]")
                console.print("[yellow]  - Run 'raccoon connect <pi-address>' to connect to a Pi[/yellow]")
                console.print("[yellow]  - Use --local to run on this machine (requires hardware)[/yellow]")
                raise SystemExit(1)

    if not manager.is_connected:
        console.print("[red]Failed to establish connection to Pi.[/red]")
        console.print("[yellow]Use --local to run on this machine instead.[/yellow]")
        raise SystemExit(1)


@click.group(name="calibrate")
@click.pass_context
def calibrate_group(ctx: click.Context) -> None:
    """Calibrate robot motors and sensors.

    Subcommands:

        motors    - Calibrate motor PID and feedforward parameters

        rpm       - Calibrate motor RPM vs power using hall effect sensor
                    (also computes BEMF scale/offset)

        benchmark - Test motor PID responsiveness and control quality
    """
    pass


@calibrate_group.command(name="motors")
@click.option("--aggressive", is_flag=True, default=False, help="Use aggressive calibration mode (relay feedback)")
@click.option("--local", "-l", is_flag=True, help="Run locally on this machine (requires hardware)")
@click.option("--yes", "-y", is_flag=True, help="Auto-save calibration results without prompting")
@click.option(
    "--export-validation/--no-export-validation",
    default=True,
    help="Export validation command vs measured velocity CSVs (default: enabled)",
)
@click.option(
    "--validation-output-dir",
    type=str,
    default=None,
    help="Directory to write validation CSVs (default: <project>/logs/motor_validation)",
)
@click.pass_context
def motors_command(
    ctx: click.Context,
    aggressive: bool,
    local: bool,
    yes: bool,
    export_validation: bool,
    validation_output_dir: Optional[str],
) -> None:
    """Calibrate motor PID and feedforward parameters.

    Runs motor calibration to determine PID and feedforward parameters.
    The results are automatically saved to raccoon.project.yml.

    By default, runs on the connected Pi. Use --local to run on this machine.
    """
    console: Console = ctx.obj["console"]

    # Always require project context
    project_root, config = _require_project_context(console)

    if local:
        # Run locally
        calibrate_motors_local(
            ctx,
            project_root,
            config,
            aggressive,
            auto_save=yes,
            export_validation=export_validation,
            validation_output_dir=validation_output_dir,
        )
    else:
        # Require remote connection
        _require_remote_connection(console, project_root)
        asyncio.run(
            calibrate_motors_remote(
                ctx,
                project_root,
                config,
                aggressive,
                export_validation=export_validation,
                validation_output_dir=validation_output_dir,
            )
        )


@calibrate_group.command(name="rpm")
@click.option("--motor-port", "-m", type=int, required=True, help="Motor port number (0-3)")
@click.option("--sensor-port", "-s", type=int, required=True, help="Hall effect sensor digital port number")
@click.option("--output", "-o", type=str, default=None, help="Output CSV file path (default: rpm_calibration_<timestamp>.csv)")
@click.option("--power-steps", type=int, default=20, help="Number of power steps from 0%% to 100%% (default: 20)")
@click.option("--rotations", "-r", type=int, default=5, help="Number of wheel rotations per power step (default: 5)")
@click.option("--magnets", type=int, default=5, help="Number of magnets on the wheel (default: 5)")
@click.option("--min-power", type=int, default=20, help="Minimum power %% to include in BEMF fit (default: 20)")
@click.option("--local", "-l", is_flag=True, help="Run locally on this machine (requires hardware)")
@click.option("--yes", "-y", is_flag=True, help="Auto-save calibration results without prompting")
@click.pass_context
def rpm_command(
    ctx: click.Context,
    motor_port: int,
    sensor_port: int,
    output: Optional[str],
    power_steps: int,
    rotations: int,
    magnets: int,
    min_power: int,
    local: bool,
    yes: bool,
) -> None:
    """Calibrate motor RPM vs power using a hall effect sensor.

    This command measures motor RPM and BEMF readings at various power levels,
    then automatically computes BEMF scale and offset calibration values.

    The calibration corrects for non-linearity in BEMF readings at different
    speeds, ensuring consistent ticks-per-revolution across all motor speeds.

    By default, runs on the connected Pi. Use --local to run on this machine.

    Setup:

    1. Mount magnets evenly spaced around the wheel (default: 5 magnets)

    2. Position the hall effect sensor to detect the magnets

    3. The motor will automatically rotate to find the magnet position

    The calibration will:

    - Step through power levels from 0%% to 100%%

    - At each level, measure time for the specified number of rotations

    - Record RPM and BEMF encoder ticks

    - Compute bemf_scale and bemf_offset from linear regression

    - Save BEMF calibration to raccoon.project.yml

    - Save raw data to a CSV file for analysis

    Examples:

        raccoon calibrate rpm -m 0 -s 5

        raccoon calibrate rpm --motor-port 0 --sensor-port 5 --output my_calibration.csv

        raccoon calibrate rpm -m 0 -s 5 --power-steps 10 --rotations 3 --min-power 30
    """
    console: Console = ctx.obj["console"]

    # Always require project context
    project_root, config = _require_project_context(console)

    # Generate default output filename if not specified
    if output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = f"rpm_calibration_{timestamp}.csv"

    output_path = Path(output)

    # Validate parameters
    if motor_port < 0 or motor_port > 3:
        console.print("[red]Motor port must be between 0 and 3[/red]")
        raise SystemExit(1)

    if power_steps < 1 or power_steps > 100:
        console.print("[red]Power steps must be between 1 and 100[/red]")
        raise SystemExit(1)

    if rotations < 1:
        console.print("[red]Rotations must be at least 1[/red]")
        raise SystemExit(1)

    if magnets < 1:
        console.print("[red]Magnets must be at least 1[/red]")
        raise SystemExit(1)

    if min_power < 0 or min_power > 100:
        console.print("[red]Min power must be between 0 and 100[/red]")
        raise SystemExit(1)

    if local:
        # Run locally
        calibrate_rpm_local(
            ctx,
            motor_port=motor_port,
            sensor_port=sensor_port,
            output_file=output_path,
            power_steps=power_steps,
            rotations_per_step=rotations,
            magnets_per_rotation=magnets,
            min_power=min_power,
            project_root=project_root,
            config=config,
            auto_save=yes,
        )
    else:
        # Require remote connection
        _require_remote_connection(console, project_root)
        asyncio.run(calibrate_rpm_remote(
            ctx, project_root, config,
            motor_port, sensor_port, output,
            power_steps, rotations, magnets, min_power
        ))


@calibrate_group.command(name="benchmark")
@click.option(
    "--power", "-p",
    type=float,
    multiple=True,
    default=[30.0, 50.0, 70.0, 100.0, -30.0, -50.0, -70.0],
    help="Motor power %% to test (can specify multiple, default: 30, 50, 70, 100, -30, -50, -70)",
)
@click.option(
    "--duration", "-d",
    type=float,
    default=2.0,
    help="Duration of each step response test in seconds (default: 2.0)",
)
@click.option(
    "--sample-rate", "-r",
    type=float,
    default=100.0,
    help="Sampling rate in Hz (default: 100)",
)
@click.option(
    "--output-dir", "-o",
    type=str,
    default=None,
    help="Output directory for results (default: <project>/logs/motor_benchmark)",
)
@click.option("--local", "-l", is_flag=True, help="Run locally on this machine (requires hardware)")
@click.pass_context
def benchmark_command(
    ctx: click.Context,
    power: tuple,
    duration: float,
    sample_rate: float,
    output_dir: Optional[str],
    local: bool,
) -> None:
    """Benchmark motor PID responsiveness and control quality.

    Tests each motor's step response characteristics including:

    - Rise time: How quickly the motor reaches target speed (10% to 90%)

    - Settling time: Time to stay within 5% of target

    - Overshoot: Peak velocity beyond target as percentage

    - Steady-state error: Average error after settling

    Each motor receives a letter grade (A-F) and numerical score (0-100)
    based on these metrics. Results are saved to CSV and plotted.

    By default, runs on the connected Pi. Use --local to run on this machine.

    Examples:

        raccoon calibrate benchmark

        raccoon calibrate benchmark -p 40 -p 60 -p 80 -p -40

        raccoon calibrate benchmark --duration 3.0 --sample-rate 200

        raccoon calibrate benchmark --local
    """
    console: Console = ctx.obj["console"]

    # Always require project context
    project_root, config = _require_project_context(console)

    # Convert tuple to list
    powers = list(power)

    # Validate parameters
    if duration <= 0:
        console.print("[red]Duration must be positive[/red]")
        raise SystemExit(1)

    if sample_rate <= 0:
        console.print("[red]Sample rate must be positive[/red]")
        raise SystemExit(1)

    if not powers:
        console.print("[red]At least one power level must be specified[/red]")
        raise SystemExit(1)

    # Validate power range
    for p in powers:
        if abs(p) > 100:
            console.print(f"[red]Power {p}%% is out of range (-100 to 100)[/red]")
            raise SystemExit(1)

    if local:
        # Run locally
        benchmark_motors_local(
            ctx,
            project_root,
            config,
            powers=powers,
            duration=duration,
            sample_rate=sample_rate,
            output_dir=output_dir,
        )
    else:
        # Require remote connection
        _require_remote_connection(console, project_root)
        asyncio.run(
            benchmark_motors_remote(
                ctx,
                project_root,
                config,
                powers=powers,
                duration=duration,
                sample_rate=sample_rate,
                output_dir=output_dir,
            )
        )


# Backwards compatibility: expose the group as calibrate_command
calibrate_command = calibrate_group
