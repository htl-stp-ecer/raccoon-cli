"""Motor PID/FF calibration."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Dict, Any

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

logger = logging.getLogger("raccoon")


def update_motor_calibration(config: Dict[str, Any], motor_name: str, calibration_result: Any) -> None:
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

    # Preserve existing calibration fields
    if "calibration" in motor_def:
        old_cal = motor_def["calibration"]
        for key in ["ticks_to_rad", "vel_lpf_alpha", "bemf_scale", "bemf_offset", "ticks_per_revolution"]:
            if key in old_cal:
                calibration[key] = old_cal[key]

    motor_def["calibration"] = calibration


def render_calibration_results(console: Console, results: list, motor_names: list[str]) -> None:
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


def calibrate_motors_local(ctx: click.Context, project_root: Path, config: dict, aggressive: bool, auto_save: bool = False) -> None:
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
        render_calibration_results(console, results, [f"Motor {i}" for i in range(len(results))])
        raise SystemExit(1)

    # Display results
    console.print()
    render_calibration_results(console, results, motor_names)

    # Check if all calibrations succeeded
    all_success = all(result.success for result in results)

    if not all_success:
        console.print("\n[yellow]⚠ Some motor calibrations failed. Configuration will not be updated.[/yellow]")
        raise SystemExit(1)

    # Update configuration
    for motor_name, result in zip(motor_names, results):
        update_motor_calibration(config, motor_name, result)

    # Save configuration
    if not auto_save:
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


async def calibrate_motors_remote(ctx: click.Context, project_root: Path, config: dict, aggressive: bool) -> None:
    """Run motor PID/FF calibration on the connected Pi."""
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

    console.print(f"[cyan]Running calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Build args for calibrate command
    # Always pass --yes for remote execution since there's no interactive stdin
    args = ["motors", "--yes"]
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

        if exit_code == 0:
            console.print()
            console.print("[green]Calibration completed on Pi![/green]")
            console.print("[dim]Syncing calibration results...[/dim]")
            if sync_project_interactive(project_root, console):
                console.print("[green]✓ Calibration results synced to local project[/green]")
            else:
                console.print("[yellow]Warning: Failed to sync results. Run 'raccoon sync' manually.[/yellow]")
        else:
            console.print()
            console.print(f"[red]Calibration failed with exit code {exit_code}[/red]")
            raise SystemExit(exit_code)
