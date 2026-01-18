"""Calibrate robot motors command."""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


def _update_motor_calibration(config: Dict[str, Any], motor_name: str, calibration_result: Any) -> None:
    """Update a single motor's calibration data in the config."""
    definitions = config.setdefault("definitions", {})

    if motor_name not in definitions:
        logger.warning(f"Motor '{motor_name}' not found in definitions")
        return

    motor_def = definitions[motor_name]

    # Add calibration data
    calibration = {
        "ff": {
            "kS": calibration_result.ff.kS,
            "kV": calibration_result.ff.kV,
            "kA": calibration_result.ff.kA,
        },
        "pid": {
            "kp": calibration_result.pid.kp,
            "ki": calibration_result.pid.ki,
            "kd": calibration_result.pid.kd,
        },
    }

    # Preserve existing ticks_to_rad and vel_lpf_alpha if present
    if "calibration" in motor_def:
        old_cal = motor_def["calibration"]
        if "ticks_to_rad" in old_cal:
            calibration["ticks_to_rad"] = old_cal["ticks_to_rad"]
        if "vel_lpf_alpha" in old_cal:
            calibration["vel_lpf_alpha"] = old_cal["vel_lpf_alpha"]

    motor_def["calibration"] = calibration


def _render_calibration_results(console: Console, results: list, motor_names: list[str]) -> None:
    """Display calibration results in a formatted table."""
    table = Table(title="Motor Calibration Results", expand=True)
    table.add_column("Motor", style="bold cyan")
    table.add_column("Status", style="bold")
    table.add_column("PID", style="dim")
    table.add_column("Feedforward", style="dim")

    for motor_name, result in zip(motor_names, results):
        if result.success:
            status = "[green]✓ Success[/green]"
            pid_str = f"kp={result.pid.kp:.4f}\nki={result.pid.ki:.4f}\nkd={result.pid.kd:.4f}"
            ff_str = f"kS={result.ff.kS:.6f}\nkV={result.ff.kV:.6f}\nkA={result.ff.kA:.6f}"
        else:
            status = "[red]✗ Failed[/red]"
            pid_str = "—"
            ff_str = "—"

        table.add_row(motor_name, status, pid_str, ff_str)

    console.print(Panel(table, border_style="green"))


def _calibrate_motors_local(ctx: click.Context, project_root: Path, config: dict, aggressive: bool) -> None:
    """Run motor PID/FF calibration locally (on the Pi itself)."""
    console: Console = ctx.obj["console"]

    # Add project root to Python path for imports
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Import required libraries
    try:
        from libstp.calibration import CalibrationConfig
        from src.hardware.robot import Robot
    except ImportError as exc:
        console.print(f"[red]Failed to import required modules: {exc}[/red]")
        console.print("[yellow]Make sure you have run 'raccoon codegen' first and that libstp is installed.[/yellow]")
        raise SystemExit(1) from exc

    console.print(Panel(
        f"[bold cyan]Starting Motor Calibration[/bold cyan]\n"
        f"Mode: {'[yellow]Aggressive (relay feedback)[/yellow]' if aggressive else '[green]Standard[/green]'}\n"
        f"Project: {config.get('name', 'Unknown')}",
        border_style="cyan"
    ))

    # Create robot instance
    try:
        robot = Robot()
    except Exception as exc:
        console.print(f"[red]Failed to initialize robot: {exc}[/red]")
        raise SystemExit(1) from exc

    # Run calibration
    console.print("\n[cyan]Running calibration... This may take a few moments.[/cyan]")

    try:
        if aggressive:
            calibration_config = CalibrationConfig()
            calibration_config.use_relay_feedback = True
            results = robot.kinematics.calibrate_motors(calibration_config)
        else:
            results = robot.kinematics.calibrate_motors()
    except Exception as exc:
        console.print(f"\n[red]Calibration failed: {exc}[/red]")
        raise SystemExit(1) from exc

    # Determine motor names based on drivetrain type
    drivetrain_type = config.get("robot", {}).get("drive", {}).get("kinematics", {}).get("type", "")

    if drivetrain_type == "mecanum":
        motor_names = ["front_left_motor", "front_right_motor", "rear_left_motor", "rear_right_motor"]
    elif drivetrain_type == "differential":
        motor_names = ["left_motor", "right_motor"]
    else:
        console.print(f"[yellow]Warning: Unknown drivetrain type '{drivetrain_type}'. Cannot update configuration.[/yellow]")
        _render_calibration_results(console, results, [f"Motor {i}" for i in range(len(results))])
        raise SystemExit(1)

    # Display results
    console.print()
    _render_calibration_results(console, results, motor_names)

    # Check if all calibrations succeeded
    all_success = all(result.success for result in results)

    if not all_success:
        console.print("\n[yellow]⚠ Some motor calibrations failed. Configuration will not be updated.[/yellow]")
        raise SystemExit(1)

    # Update configuration
    for motor_name, result in zip(motor_names, results):
        _update_motor_calibration(config, motor_name, result)

    # Save configuration
    if not click.confirm("\nSave calibration results to raccoon.project.yml?", default=True):
        console.print("[yellow]Calibration results not saved.[/yellow]")
        return

    config_path = project_root / "raccoon.project.yml"

    try:
        with open(config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=False, default_flow_style=False)

        console.print(f"\n[green]✓ Calibration results saved to {config_path.relative_to(project_root)}[/green]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save configuration: {exc}[/red]")
        raise SystemExit(1) from exc


async def _calibrate_motors_remote(ctx: click.Context, project_root: Path, config: dict, aggressive: bool) -> None:
    """Run motor PID/FF calibration on the connected Pi."""
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.commands.sync_cmd import sync_project_to_pi

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Sync project first
    console.print("[dim]Syncing project...[/dim]")
    if not sync_project_to_pi(project_root, console):
        console.print("[red]Failed to sync project to Pi[/red]")
        raise SystemExit(1)
    console.print("[dim]Sync complete[/dim]")
    console.print()

    # Build args for calibrate command
    args = ["motors"]
    if aggressive:
        args.append("--aggressive")

    # Start the calibrate command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start calibration on Pi: {e}[/red]")
            raise SystemExit(1)

        # Stream output via WebSocket (URL includes auth token)
        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print()

        final_status = handler.stream_to_console(console)

        # Display final status
        exit_code = final_status.get("exit_code", -1)
        status = final_status.get("status", "unknown")

        if exit_code == 0:
            console.print()
            console.print("[green]Calibration completed on Pi![/green]")
            console.print("[yellow]Note: The calibration results are saved on the Pi.[/yellow]")
            console.print("[yellow]Use 'raccoon sync' to pull the updated config back to your laptop.[/yellow]")
        else:
            console.print()
            console.print(f"[red]Calibration failed with exit code {exit_code}[/red]")
            raise SystemExit(exit_code)


# =============================================================================
# RPM Calibration Functions
# =============================================================================


def _wait_for_sensor_trigger(sensor, timeout: float = 30.0) -> bool:
    """
    Wait for the hall effect sensor to trigger (go from False to True).

    Returns True if triggered, False if timeout.
    """
    start_time = time.time()
    # Wait for sensor to go low first (if it's high)
    while sensor.read():
        if time.time() - start_time > timeout:
            return False
        time.sleep(0.001)

    # Wait for sensor to go high (magnet detected)
    while not sensor.read():
        if time.time() - start_time > timeout:
            return False
        time.sleep(0.001)

    return True


def _count_rotations_fast(
    sensor,
    motor,
    num_rotations: int,
    magnets_per_rotation: int,
    timeout: float = 30.0,
) -> dict:
    """
    Count rotations using hall effect sensor with timing analysis.

    Returns dict with:
        - elapsed: total time in seconds
        - bemf_delta: encoder ticks moved
        - trigger_times: list of timestamps for each magnet detection
        - intervals: list of intervals between triggers
        - anomalies: list of detected anomalies
    """
    total_triggers = num_rotations * magnets_per_rotation
    trigger_count = 0
    trigger_times = []

    start_time = time.perf_counter()  # High-resolution timer
    start_bemf = motor.get_position()
    last_sensor_state = sensor.read()

    # Tight polling loop - no sleep for maximum speed
    while trigger_count < total_triggers:
        now = time.perf_counter()
        if now - start_time > timeout:
            break

        current_state = sensor.read()

        # Detect rising edge (False -> True)
        if current_state and not last_sensor_state:
            trigger_times.append(now - start_time)
            trigger_count += 1

        last_sensor_state = current_state

    end_time = time.perf_counter()
    end_bemf = motor.get_position()

    elapsed = end_time - start_time
    bemf_delta = abs(end_bemf - start_bemf)

    # Calculate intervals between triggers
    intervals = []
    if len(trigger_times) > 1:
        for i in range(1, len(trigger_times)):
            intervals.append(trigger_times[i] - trigger_times[i - 1])

    # Detect anomalies in periodicity
    anomalies = []
    if len(intervals) >= 3:
        median_interval = sorted(intervals)[len(intervals) // 2]
        for i, interval in enumerate(intervals):
            # Flag if interval deviates more than 30% from median
            deviation = abs(interval - median_interval) / median_interval if median_interval > 0 else 0
            if deviation > 0.30:
                anomalies.append({
                    "trigger_index": i + 1,
                    "interval": interval,
                    "expected": median_interval,
                    "deviation_percent": round(deviation * 100, 1),
                })

    return {
        "elapsed": elapsed,
        "bemf_delta": bemf_delta,
        "trigger_times": trigger_times,
        "intervals": intervals,
        "anomalies": anomalies,
        "trigger_count": trigger_count,
    }


def _calibrate_rpm_local(
    ctx: click.Context,
    motor_port: int,
    sensor_port: int,
    output_file: Path,
    power_steps: int,
    rotations_per_step: int,
    magnets_per_rotation: int,
) -> None:
    """Run RPM calibration locally (on the Pi itself)."""
    console: Console = ctx.obj["console"]

    # Import required libraries
    try:
        from libstp.hal import Motor, DigitalSensor
    except ImportError as exc:
        console.print(f"[red]Failed to import libstp: {exc}[/red]")
        console.print("[yellow]Make sure libstp is installed and you're running on the Pi.[/yellow]")
        raise SystemExit(1) from exc

    console.print(Panel(
        f"[bold cyan]RPM Calibration[/bold cyan]\n\n"
        f"Motor port: [yellow]{motor_port}[/yellow]\n"
        f"Hall sensor port: [yellow]{sensor_port}[/yellow]\n"
        f"Power steps: [yellow]0% to 100% in {power_steps} steps[/yellow]\n"
        f"Rotations per step: [yellow]{rotations_per_step}[/yellow]\n"
        f"Magnets per rotation: [yellow]{magnets_per_rotation}[/yellow]\n"
        f"Output file: [yellow]{output_file}[/yellow]",
        border_style="cyan"
    ))

    # Initialize hardware
    try:
        motor = Motor(port=motor_port, inverted=False)
        sensor = DigitalSensor(port=sensor_port)
    except Exception as exc:
        console.print(f"[red]Failed to initialize hardware: {exc}[/red]")
        raise SystemExit(1) from exc

    # Initial setup: automatically rotate wheel slowly to find magnet
    console.print("\n[bold yellow]Initial Setup[/bold yellow]")
    console.print("Automatically rotating wheel to find magnet position...")
    console.print("The motor will rotate slowly until the hall sensor detects a magnet.\n")

    # Rotate motor slowly to find magnet
    SEARCH_POWER = 15  # Very slow rotation speed (15% power)
    SEARCH_TIMEOUT = 30.0  # Maximum time to search

    console.print(f"[dim]Rotating at {SEARCH_POWER}% power to find magnet...[/dim]")

    motor.set_speed(SEARCH_POWER)
    search_start = time.time()
    magnet_found = False

    try:
        while time.time() - search_start < SEARCH_TIMEOUT:
            state = sensor.read()
            if state:
                # Magnet detected - stop motor immediately
                motor.set_speed(0)
                time.sleep(0.1)
                # Confirm detection is stable
                if sensor.read():
                    magnet_found = True
                    break
                else:
                    # False positive, continue searching
                    motor.set_speed(SEARCH_POWER)
            time.sleep(0.005)  # 5ms polling
    finally:
        motor.set_speed(0)

    if not magnet_found:
        console.print("\n[red]Timeout: Could not find magnet within 30 seconds.[/red]")
        console.print("[yellow]Please check that the hall sensor is correctly positioned and magnets are installed.[/yellow]")
        raise SystemExit(1)

    console.print("\n[green]✓ Magnet detected! Starting calibration...[/green]\n")
    time.sleep(0.2)  # Brief pause before starting

    # Calculate power levels
    power_levels = [int(i * 100 / power_steps) for i in range(power_steps + 1)]
    # Remove 0% as motor won't move
    power_levels = [p for p in power_levels if p > 0]

    results = []
    all_anomalies = []

    # Create results table
    table = Table(title="RPM Calibration Results")
    table.add_column("Power %", justify="right", style="cyan")
    table.add_column("Time (s)", justify="right")
    table.add_column("RPM", justify="right", style="green")
    table.add_column("BEMF Ticks", justify="right")
    table.add_column("Ticks/Rev", justify="right", style="yellow")
    table.add_column("Status", justify="center")

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        ) as progress:
            task = progress.add_task("Calibrating...", total=len(power_levels))

            for power in power_levels:
                progress.update(task, description=f"Testing {power}% power...")

                # Set motor power
                motor.set_speed(power)

                # Brief wait for motor to reach steady state (reduced from 0.5s)
                time.sleep(0.15)

                # Count rotations and measure time with anomaly detection
                measurement = _count_rotations_fast(
                    sensor=sensor,
                    motor=motor,
                    num_rotations=rotations_per_step,
                    magnets_per_rotation=magnets_per_rotation,
                    timeout=30.0,
                )

                elapsed = measurement["elapsed"]
                bemf_delta = measurement["bemf_delta"]
                anomalies = measurement["anomalies"]
                intervals = measurement["intervals"]

                # Calculate RPM
                if elapsed > 0:
                    rpm = (rotations_per_step / elapsed) * 60.0
                    ticks_per_rev = bemf_delta / rotations_per_step if rotations_per_step > 0 else 0
                else:
                    rpm = 0
                    ticks_per_rev = 0

                # Calculate interval statistics
                interval_std = 0
                interval_mean = 0
                if intervals:
                    interval_mean = sum(intervals) / len(intervals)
                    if len(intervals) > 1:
                        variance = sum((x - interval_mean) ** 2 for x in intervals) / len(intervals)
                        interval_std = variance ** 0.5

                # Store result
                result = {
                    "power_percent": power,
                    "time_seconds": round(elapsed, 4),
                    "rpm": round(rpm, 2),
                    "bemf_ticks": bemf_delta,
                    "ticks_per_revolution": round(ticks_per_rev, 2),
                    "interval_mean_ms": round(interval_mean * 1000, 3),
                    "interval_std_ms": round(interval_std * 1000, 3),
                    "anomaly_count": len(anomalies),
                }
                results.append(result)

                # Track anomalies with power level
                for anomaly in anomalies:
                    anomaly["power_percent"] = power
                    all_anomalies.append(anomaly)

                # Determine status
                if anomalies:
                    status = f"[yellow]⚠ {len(anomalies)} anomalies[/yellow]"
                else:
                    status = "[green]✓[/green]"

                # Add to display table
                table.add_row(
                    str(power),
                    f"{elapsed:.3f}",
                    f"{rpm:.1f}",
                    str(bemf_delta),
                    f"{ticks_per_rev:.1f}",
                    status,
                )

                progress.advance(task)

    finally:
        # Always stop the motor
        motor.set_speed(0)
        console.print("\n[dim]Motor stopped.[/dim]")

    # Display results
    console.print()
    console.print(table)

    # Report anomalies if any
    if all_anomalies:
        console.print()
        anomaly_table = Table(title="[yellow]Detected Anomalies[/yellow]", border_style="yellow")
        anomaly_table.add_column("Power %", justify="right", style="cyan")
        anomaly_table.add_column("Trigger #", justify="right")
        anomaly_table.add_column("Interval (ms)", justify="right")
        anomaly_table.add_column("Expected (ms)", justify="right")
        anomaly_table.add_column("Deviation", justify="right", style="red")

        for anomaly in all_anomalies[:20]:  # Limit to first 20
            anomaly_table.add_row(
                str(anomaly["power_percent"]),
                str(anomaly["trigger_index"]),
                f"{anomaly['interval'] * 1000:.2f}",
                f"{anomaly['expected'] * 1000:.2f}",
                f"{anomaly['deviation_percent']:.1f}%",
            )

        if len(all_anomalies) > 20:
            console.print(f"[dim]Showing first 20 of {len(all_anomalies)} anomalies[/dim]")

        console.print(anomaly_table)

    # Write CSV file
    try:
        with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
            fieldnames = [
                "power_percent", "time_seconds", "rpm", "bemf_ticks",
                "ticks_per_revolution", "interval_mean_ms", "interval_std_ms", "anomaly_count"
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        console.print(f"\n[green]✓ Results saved to {output_file}[/green]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save CSV: {exc}[/red]")
        raise SystemExit(1) from exc

    # Summary statistics
    if results:
        max_rpm = max(r["rpm"] for r in results)
        avg_ticks = sum(r["ticks_per_revolution"] for r in results) / len(results)
        total_anomalies = sum(r["anomaly_count"] for r in results)

        summary_lines = [
            f"[bold]Summary[/bold]\n",
            f"Maximum RPM: [green]{max_rpm:.1f}[/green]",
            f"Average ticks/revolution: [yellow]{avg_ticks:.1f}[/yellow]",
            f"Data points collected: [cyan]{len(results)}[/cyan]",
        ]

        if total_anomalies > 0:
            summary_lines.append(f"Total anomalies: [red]{total_anomalies}[/red]")
        else:
            summary_lines.append("Timing consistency: [green]Good (no anomalies)[/green]")

        console.print(Panel(
            "\n".join(summary_lines),
            border_style="green" if total_anomalies == 0 else "yellow"
        ))


async def _calibrate_rpm_remote(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    motor_port: int,
    sensor_port: int,
    output_file: str,
    power_steps: int,
    rotations_per_step: int,
    magnets_per_rotation: int,
) -> None:
    """Run RPM calibration on the connected Pi."""
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.commands.sync_cmd import sync_project_to_pi

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running RPM calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Sync project first
    console.print("[dim]Syncing project...[/dim]")
    if not sync_project_to_pi(project_root, console):
        console.print("[red]Failed to sync project to Pi[/red]")
        raise SystemExit(1)
    console.print("[dim]Sync complete[/dim]")
    console.print()

    # Build args for calibrate rpm command
    args = [
        "rpm",
        "--motor-port", str(motor_port),
        "--sensor-port", str(sensor_port),
        "--output", output_file,
        "--power-steps", str(power_steps),
        "--rotations", str(rotations_per_step),
        "--magnets", str(magnets_per_rotation),
    ]

    # Start the calibrate command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start RPM calibration on Pi: {e}[/red]")
            raise SystemExit(1)

        # Stream output via WebSocket
        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print()

        final_status = handler.stream_to_console(console)

        exit_code = final_status.get("exit_code", -1)

        if exit_code == 0:
            console.print()
            console.print("[green]RPM calibration completed on Pi![/green]")
            console.print(f"[yellow]Note: Results saved to {output_file} on the Pi.[/yellow]")
            console.print("[yellow]Use 'raccoon sync' to pull the results back to your laptop.[/yellow]")
        else:
            console.print()
            console.print(f"[red]RPM calibration failed with exit code {exit_code}[/red]")
            raise SystemExit(exit_code)


# =============================================================================
# CLI Command Definitions
# =============================================================================


@click.group(name="calibrate")
@click.pass_context
def calibrate_group(ctx: click.Context) -> None:
    """Calibrate robot motors and sensors.

    Subcommands:

        motors  - Calibrate motor PID and feedforward parameters

        rpm     - Calibrate motor RPM vs power using hall effect sensor
    """
    pass


@calibrate_group.command(name="motors")
@click.option("--aggressive", is_flag=True, default=False, help="Use aggressive calibration mode (relay feedback)")
@click.option("--local", "-l", is_flag=True, help="Force local execution (requires hardware)")
@click.pass_context
def motors_command(ctx: click.Context, aggressive: bool, local: bool) -> None:
    """Calibrate motor PID and feedforward parameters.

    Runs motor calibration to determine PID and feedforward parameters.
    The results are automatically saved to raccoon.project.yml.

    If connected to a Pi, runs calibration remotely.
    Use --local to force local execution (requires robot hardware).
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    try:
        config = load_project_config(project_root)
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    # Check if we should run remotely
    if not local:
        from raccoon.client.connection import get_connection_manager

        manager = get_connection_manager()

        # Try to auto-connect from project or global config if not connected
        if not manager.is_connected:
            # Try project config first
            project_conn = manager.load_from_project(project_root)
            if project_conn and project_conn.pi_address:
                logger.info(f"Connecting to Pi from project config: {project_conn.pi_address}")
                manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
            else:
                # Try global config
                known_pis = manager.load_known_pis()
                if known_pis:
                    pi = known_pis[0]
                    logger.info(f"Connecting to known Pi: {pi.get('address')}")
                    manager.connect_sync(pi.get("address"), pi.get("port", 8421))

        if manager.is_connected:
            # Run remotely
            asyncio.run(_calibrate_motors_remote(ctx, project_root, config, aggressive))
            return

    # Run locally
    _calibrate_motors_local(ctx, project_root, config, aggressive)


@calibrate_group.command(name="rpm")
@click.option("--motor-port", "-m", type=int, required=True, help="Motor port number (0-3)")
@click.option("--sensor-port", "-s", type=int, required=True, help="Hall effect sensor digital port number")
@click.option("--output", "-o", type=str, default=None, help="Output CSV file path (default: rpm_calibration_<timestamp>.csv)")
@click.option("--power-steps", type=int, default=20, help="Number of power steps from 0%% to 100%% (default: 20)")
@click.option("--rotations", "-r", type=int, default=5, help="Number of wheel rotations per power step (default: 5)")
@click.option("--magnets", type=int, default=5, help="Number of magnets on the wheel (default: 5)")
@click.option("--local", "-l", is_flag=True, help="Force local execution (requires hardware)")
@click.pass_context
def rpm_command(
    ctx: click.Context,
    motor_port: int,
    sensor_port: int,
    output: Optional[str],
    power_steps: int,
    rotations: int,
    magnets: int,
    local: bool,
) -> None:
    """Calibrate motor RPM vs power using a hall effect sensor.

    This command measures motor RPM and BEMF readings at various power levels.
    The wheel must have magnets mounted at regular spacing, detected by a hall
    effect sensor connected to a digital port.

    Setup:

    1. Mount magnets evenly spaced around the wheel (default: 5 magnets)

    2. Position the hall effect sensor to detect the magnets

    3. Before starting, rotate the wheel so a magnet is directly under the sensor

    The calibration will:

    - Step through power levels from 0%% to 100%%

    - At each level, measure time for the specified number of rotations

    - Record RPM and BEMF encoder ticks

    - Save all data to a CSV file for analysis

    Examples:

        raccoon calibrate rpm -m 0 -s 5

        raccoon calibrate rpm --motor-port 0 --sensor-port 5 --output my_calibration.csv

        raccoon calibrate rpm -m 0 -s 5 --power-steps 10 --rotations 3
    """
    console: Console = ctx.obj["console"]

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

    # Check if we need project context for remote execution
    project_root = None
    config = None

    try:
        project_root = require_project()
        config = load_project_config(project_root)
    except ProjectError:
        # No project context - force local mode
        local = True

    # Check if we should run remotely
    if not local and project_root and config:
        from raccoon.client.connection import get_connection_manager

        manager = get_connection_manager()

        # Try to auto-connect
        if not manager.is_connected:
            project_conn = manager.load_from_project(project_root)
            if project_conn and project_conn.pi_address:
                logger.info(f"Connecting to Pi from project config: {project_conn.pi_address}")
                manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
            else:
                known_pis = manager.load_known_pis()
                if known_pis:
                    pi = known_pis[0]
                    logger.info(f"Connecting to known Pi: {pi.get('address')}")
                    manager.connect_sync(pi.get("address"), pi.get("port", 8421))

        if manager.is_connected:
            # Run remotely
            asyncio.run(_calibrate_rpm_remote(
                ctx, project_root, config,
                motor_port, sensor_port, output,
                power_steps, rotations, magnets
            ))
            return

    # Run locally
    _calibrate_rpm_local(
        ctx,
        motor_port=motor_port,
        sensor_port=sensor_port,
        output_file=output_path,
        power_steps=power_steps,
        rotations_per_step=rotations,
        magnets_per_rotation=magnets,
    )


# Backwards compatibility: expose the group as calibrate_command
calibrate_command = calibrate_group
