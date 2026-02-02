"""RPM calibration with automatic BEMF scale/offset computation."""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from .utils import count_rotations_fast, find_motor_by_port, save_project_config
from .bemf import (
    CalibrationPoint,
    BEMFCalibrationResult,
    fit_bemf_calibration,
)

logger = logging.getLogger("raccoon")


def _collect_rpm_data(
    console: Console,
    motor,
    sensor,
    power_levels: List[int],
    rotations_per_step: int,
    magnets_per_rotation: int,
) -> List[Dict[str, Any]]:
    """
    Collect RPM calibration data at various power levels.

    Returns list of result dicts for each power level.
    """
    results = []
    all_anomalies = []

    # Create results table for display
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

                # Brief wait for motor to reach steady state
                time.sleep(0.15)

                # Count rotations and measure time with anomaly detection
                # Scale timeout based on power - lower power = slower rotation
                scaled_timeout = max(30.0, 300.0 * (1.0 - power / 100.0) + 30.0)
                measurement = count_rotations_fast(
                    sensor=sensor,
                    motor=motor,
                    num_rotations=rotations_per_step,
                    magnets_per_rotation=magnets_per_rotation,
                    timeout=scaled_timeout,
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
                    "revolutions": rotations_per_step,
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

    # Display results table
    console.print()
    console.print(table)

    # Report anomalies if any
    if all_anomalies:
        _display_anomalies(console, all_anomalies)

    return results


def _display_anomalies(console: Console, anomalies: List[dict]) -> None:
    """Display anomaly table."""
    console.print()
    anomaly_table = Table(title="[yellow]Detected Anomalies[/yellow]", border_style="yellow")
    anomaly_table.add_column("Power %", justify="right", style="cyan")
    anomaly_table.add_column("Trigger #", justify="right")
    anomaly_table.add_column("Interval (ms)", justify="right")
    anomaly_table.add_column("Expected (ms)", justify="right")
    anomaly_table.add_column("Deviation", justify="right", style="red")

    for anomaly in anomalies[:20]:  # Limit to first 20
        anomaly_table.add_row(
            str(anomaly["power_percent"]),
            str(anomaly["trigger_index"]),
            f"{anomaly['interval'] * 1000:.2f}",
            f"{anomaly['expected'] * 1000:.2f}",
            f"{anomaly['deviation_percent']:.1f}%",
        )

    if len(anomalies) > 20:
        console.print(f"[dim]Showing first 20 of {len(anomalies)} anomalies[/dim]")

    console.print(anomaly_table)


def _compute_bemf_calibration(
    console: Console,
    results: List[Dict[str, Any]],
    min_power: int = 20,
) -> Optional[BEMFCalibrationResult]:
    """Compute BEMF calibration from collected data."""
    # Convert to CalibrationPoints
    points = []
    for r in results:
        point = CalibrationPoint(
            power_percent=r["power_percent"],
            time_seconds=r["time_seconds"],
            revolutions=r["revolutions"],
            bemf_ticks=r["bemf_ticks"],
            rpm=r["rpm"],
            tick_rate=r["bemf_ticks"] / r["time_seconds"] if r["time_seconds"] > 0 else 0,
            ticks_per_rev=r["ticks_per_revolution"],
        )
        points.append(point)

    try:
        bemf_result = fit_bemf_calibration(points, min_power=min_power)
        return bemf_result
    except ValueError as e:
        console.print(f"\n[yellow]Warning: Could not compute BEMF calibration: {e}[/yellow]")
        return None


def _display_bemf_results(console: Console, bemf_result: BEMFCalibrationResult) -> None:
    """Display BEMF calibration results."""
    console.print()
    console.print(Panel(
        f"[bold cyan]BEMF Calibration Results[/bold cyan]\n\n"
        f"[green]bemf_scale:[/green] {bemf_result.bemf_scale:.6f}\n"
        f"[green]bemf_offset:[/green] {bemf_result.bemf_offset:.6f}\n"
        f"[green]ticks_per_revolution:[/green] {bemf_result.ticks_per_revolution:.2f}\n\n"
        f"[dim]R² = {bemf_result.r_squared:.4f} (fit quality)[/dim]\n"
        f"[dim]Data points used: {bemf_result.data_points_used}[/dim]",
        border_style="green" if bemf_result.r_squared > 0.95 else "yellow",
    ))


def _update_motor_bemf_calibration(
    config: Dict[str, Any],
    motor_name: str,
    bemf_result: BEMFCalibrationResult,
) -> bool:
    """Update motor's BEMF calibration in config. Returns True if updated."""
    definitions = config.get("definitions", {})

    if motor_name not in definitions:
        return False

    motor_def = definitions[motor_name]

    if motor_def.get("type") != "Motor":
        return False

    # Ensure calibration dict exists
    if "calibration" not in motor_def:
        motor_def["calibration"] = {}

    calibration = motor_def["calibration"]

    # Update BEMF values
    calibration["bemf_scale"] = round(bemf_result.bemf_scale, 6)
    calibration["bemf_offset"] = round(bemf_result.bemf_offset, 6)
    calibration["ticks_per_revolution"] = round(bemf_result.ticks_per_revolution, 2)

    return True


def calibrate_rpm_local(
    ctx: click.Context,
    motor_port: int,
    sensor_port: int,
    output_file: Path,
    power_steps: int,
    rotations_per_step: int,
    magnets_per_rotation: int,
    min_power: int,
    project_root: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
    auto_save: bool = False,
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
        f"Min power for BEMF fit: [yellow]{min_power}%[/yellow]\n"
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

    # Find magnet position
    if not _find_magnet_position(console, motor, sensor):
        raise SystemExit(1)

    # Calculate power levels
    power_levels = [int(i * 100 / power_steps) for i in range(power_steps + 1)]
    power_levels = [p for p in power_levels if p > 0]  # Remove 0%

    # Collect data
    results = _collect_rpm_data(
        console,
        motor,
        sensor,
        power_levels,
        rotations_per_step,
        magnets_per_rotation,
    )

    if not results:
        console.print("[red]No data collected.[/red]")
        raise SystemExit(1)

    # Compute BEMF calibration
    bemf_result = _compute_bemf_calibration(console, results, min_power=min_power)

    if bemf_result:
        _display_bemf_results(console, bemf_result)

    # Write CSV file
    _save_csv_results(console, output_file, results)

    # Display summary
    _display_summary(console, results)

    # Save to YAML if we have project context and BEMF result
    if bemf_result and project_root and config:
        _save_to_yaml(console, project_root, config, motor_port, bemf_result, auto_save=auto_save)


def _find_magnet_position(console: Console, motor, sensor) -> bool:
    """Rotate motor slowly to find magnet. Returns True if found."""
    console.print("\n[bold yellow]Initial Setup[/bold yellow]")
    console.print("Automatically rotating wheel to find magnet position...")

    SEARCH_POWER = 15
    SEARCH_TIMEOUT = 30.0

    console.print(f"[dim]Rotating at {SEARCH_POWER}% power to find magnet...[/dim]")

    motor.set_speed(SEARCH_POWER)
    search_start = time.time()
    magnet_found = False

    try:
        while time.time() - search_start < SEARCH_TIMEOUT:
            state = sensor.read()
            if state:
                motor.set_speed(0)
                time.sleep(0.1)
                if sensor.read():
                    magnet_found = True
                    break
                else:
                    motor.set_speed(SEARCH_POWER)
            time.sleep(0.005)
    finally:
        motor.set_speed(0)

    if not magnet_found:
        console.print("\n[red]Timeout: Could not find magnet within 30 seconds.[/red]")
        console.print("[yellow]Please check that the hall sensor is correctly positioned and magnets are installed.[/yellow]")
        return False

    console.print("\n[green]✓ Magnet detected! Starting calibration...[/green]\n")
    time.sleep(0.2)
    return True


def _save_csv_results(console: Console, output_file: Path, results: List[Dict[str, Any]]) -> None:
    """Save results to CSV file."""
    try:
        with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
            fieldnames = [
                "power_percent", "time_seconds", "rpm", "bemf_ticks",
                "ticks_per_revolution", "interval_mean_ms", "interval_std_ms", "anomaly_count"
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)

        console.print(f"\n[green]✓ CSV results saved to {output_file}[/green]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save CSV: {exc}[/red]")


def _display_summary(console: Console, results: List[Dict[str, Any]]) -> None:
    """Display calibration summary."""
    if not results:
        return

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


def _save_to_yaml(
    console: Console,
    project_root: Path,
    config: Dict[str, Any],
    motor_port: int,
    bemf_result: BEMFCalibrationResult,
    auto_save: bool = False,
) -> None:
    """Save BEMF calibration to project YAML."""
    motor_name = find_motor_by_port(config, motor_port)

    if not motor_name:
        console.print(f"\n[yellow]Warning: No motor definition found for port {motor_port}.[/yellow]")
        console.print("[yellow]BEMF calibration not saved to config. Add motor to definitions first.[/yellow]")
        return

    console.print(f"\n[cyan]Found motor '{motor_name}' for port {motor_port}[/cyan]")

    if not auto_save:
        if not click.confirm(f"Save BEMF calibration for '{motor_name}' to raccoon.project.yml?", default=True):
            console.print("[yellow]BEMF calibration not saved.[/yellow]")
            return

    if not _update_motor_bemf_calibration(config, motor_name, bemf_result):
        console.print(f"[red]Failed to update calibration for '{motor_name}'[/red]")
        return

    try:
        save_project_config(config, project_root)
        console.print(f"\n[green]✓ BEMF calibration saved for '{motor_name}'[/green]")
        console.print(f"[dim]  bemf_scale: {bemf_result.bemf_scale:.6f}[/dim]")
        console.print(f"[dim]  bemf_offset: {bemf_result.bemf_offset:.6f}[/dim]")
        console.print(f"[dim]  ticks_per_revolution: {bemf_result.ticks_per_revolution:.2f}[/dim]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save configuration: {exc}[/red]")


async def calibrate_rpm_remote(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    motor_port: int,
    sensor_port: int,
    output_file: str,
    power_steps: int,
    rotations_per_step: int,
    magnets_per_rotation: int,
    min_power: int,
) -> None:
    """Run RPM calibration on the connected Pi."""
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.commands.sync_cmd import sync_project_interactive

    # Sync project first (with interactive conflict resolution)
    if not sync_project_interactive(project_root, console):
        console.print("[red]Cannot run calibration with unresolved conflicts[/red]")
        raise SystemExit(1)
    console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running RPM calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Build args for calibrate rpm command
    # Always pass --yes for remote execution since there's no interactive stdin
    args = [
        "rpm",
        "--yes",
        "--motor-port", str(motor_port),
        "--sensor-port", str(sensor_port),
        "--output", output_file,
        "--power-steps", str(power_steps),
        "--rotations", str(rotations_per_step),
        "--magnets", str(magnets_per_rotation),
        "--min-power", str(min_power),
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

    console.print()
    console.print("[dim]Syncing calibration results...[/dim]")
    if sync_project_interactive(project_root, console):
        console.print("[green]✓ Calibration results synced to local project[/green]")
        console.print(f"[dim]Note: CSV results saved to {output_file} on the Pi.[/dim]")
    else:
        console.print("[yellow]Warning: Failed to sync results. Run 'raccoon sync' manually.[/yellow]")

    if exit_code == 0:
        console.print()
        console.print("[green]RPM calibration completed on Pi![/green]")
        return

    console.print()
    console.print(f"[red]RPM calibration failed with exit code {exit_code}[/red]")
    raise SystemExit(exit_code)
