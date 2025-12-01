"""Calibrate robot motors command."""

from __future__ import annotations

import logging
import sys
from typing import Dict, Any

import click
import yaml
from rich.console import Console
from rich.panel import Panel
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


@click.command(name="calibrate")
@click.option("--aggressive", is_flag=True, default=False, help="Use aggressive calibration mode (relay feedback)")
@click.pass_context
def calibrate_command(ctx: click.Context, aggressive: bool) -> None:
    """Calibrate robot motors and update configuration.

    Runs motor calibration to determine PID and feedforward parameters.
    The results are automatically saved to raccoon.project.yml.
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

