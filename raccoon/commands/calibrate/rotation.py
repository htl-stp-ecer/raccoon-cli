"""Robot rotation speed calibration using IMU gyroscope."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .utils import save_project_config

logger = logging.getLogger("raccoon")


def _get_gyro_reading(imu) -> float:
    """
    Get gyroscope Z-axis reading (angular velocity around vertical axis).
    
    Returns angular velocity in rad/s.
    """
    try:
        # Try different possible IMU interfaces
        if hasattr(imu, 'get_gyro_z'):
            return imu.get_gyro_z()
        elif hasattr(imu, 'get_angular_velocity'):
            # Might return a tuple/array (x, y, z)
            vel = imu.get_angular_velocity()
            if isinstance(vel, (tuple, list)) and len(vel) >= 3:
                return vel[2]  # Z-axis
            return vel
        elif hasattr(imu, 'gyro_z'):
            return imu.gyro_z
        elif hasattr(imu, 'angular_velocity'):
            vel = imu.angular_velocity
            if isinstance(vel, (tuple, list)) and len(vel) >= 3:
                return vel[2]
            return vel
        else:
            logger.warning("Could not find gyro reading method on IMU")
            return 0.0
    except Exception as e:
        logger.warning(f"Failed to read gyro: {e}")
        return 0.0


def _test_rotation_speed(
    console: Console,
    robot,
    omega: float,
    duration: float,
    direction_name: str,
) -> tuple[float, List[float]]:
    """
    Test robot rotation at specified angular velocity.
    
    Args:
        console: Rich console for output
        robot: Robot instance with drive and IMU
        omega: Angular velocity command (rad/s)
        duration: Test duration in seconds
        direction_name: "clockwise" or "counter-clockwise"
    
    Returns:
        Tuple of (average_angular_velocity, all_samples)
    """
    from libstp.foundation import ChassisVelocity
    
    console.print(f"  Testing {direction_name} at {omega:.2f} rad/s for {duration}s...")
    
    # Start rotating
    robot.drive.set_velocity(ChassisVelocity(0, 0, omega))
    
    # Allow brief time for acceleration
    time.sleep(0.5)
    
    # Collect samples at ~50 Hz (20ms interval)
    sample_interval = 0.02  # 50 Hz
    samples = []
    start_time = time.time()
    
    while time.time() - start_time < duration:
        gyro_z = _get_gyro_reading(robot.defs.imu)
        samples.append(abs(gyro_z))  # Use absolute value
        time.sleep(sample_interval)
    
    # Stop rotation
    robot.drive.set_velocity(ChassisVelocity(0, 0, 0))
    
    # Calculate average, excluding first and last 1 second to avoid transients
    samples_per_second = int(1.0 / sample_interval)
    if len(samples) > 2 * samples_per_second:
        # Trim first and last second
        trimmed_samples = samples[samples_per_second:-samples_per_second]
    else:
        trimmed_samples = samples
    
    if trimmed_samples:
        avg_velocity = sum(trimmed_samples) / len(trimmed_samples)
    else:
        avg_velocity = 0.0
    
    console.print(f"    Samples collected: {len(samples)}, used: {len(trimmed_samples)}")
    console.print(f"    Average: {avg_velocity:.3f} rad/s")
    
    return avg_velocity, samples


def _collect_rotation_data(
    console: Console,
    config: Dict[str, Any],
    duration: float,
) -> Dict[str, Any]:
    """
    Collect rotation speed data by spinning the robot.
    
    Returns dict with cw_speed, ccw_speed, and success flag.
    """
    # Import hardware library
    try:
        from libstp.foundation import ChassisVelocity
        from src.hardware.robot import Robot
    except ImportError as exc:
        console.print(f"[red]Failed to import robot: {exc}[/red]")
        console.print("[yellow]Make sure 'raccoon codegen' has been run and you're on the Pi.[/yellow]")
        raise SystemExit(1) from exc
    
    # Check if robot has drive system
    if not hasattr(Robot, 'drive'):
        console.print("[red]Robot does not have a drive system configured.[/red]")
        console.print("[yellow]Configure a drivetrain in raccoon.project.yml first.[/yellow]")
        raise SystemExit(1)
    
    # Get max omega from config
    max_omega = config.get("robot", {}).get("drive", {}).get("limits", {}).get("max_omega", 10.0)
    
    console.print(f"\n[cyan]Commanded angular velocity: {max_omega:.2f} rad/s[/cyan]")
    console.print("[yellow]⚠ Robot will spin in place - ensure clear space![/yellow]\n")
    
    # Wait for user confirmation
    time.sleep(2)
    
    try:
        # Initialize robot
        robot = Robot()
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Testing rotation...", total=2)
            
            # Test clockwise
            progress.update(task, description="Testing clockwise rotation...")
            cw_speed, cw_samples = _test_rotation_speed(
                console, robot, max_omega, duration, "clockwise"
            )
            progress.advance(task)
            
            # Brief pause
            time.sleep(1.0)
            
            # Test counter-clockwise
            progress.update(task, description="Testing counter-clockwise rotation...")
            ccw_speed, ccw_samples = _test_rotation_speed(
                console, robot, -max_omega, duration, "counter-clockwise"
            )
            progress.advance(task)
        
        # Ensure stopped
        from libstp.foundation import ChassisVelocity
        robot.drive.set_velocity(ChassisVelocity(0, 0, 0))
        
        return {
            "cw_speed": cw_speed,
            "ccw_speed": ccw_speed,
            "cw_samples": len(cw_samples),
            "ccw_samples": len(ccw_samples),
            "commanded_omega": max_omega,
            "success": True,
        }
        
    except Exception as exc:
        console.print(f"\n[red]Error during rotation test: {exc}[/red]")
        # Try to stop robot
        try:
            from libstp.foundation import ChassisVelocity
            robot.drive.set_velocity(ChassisVelocity(0, 0, 0))
        except Exception:
            pass
        
        return {
            "cw_speed": 0.0,
            "ccw_speed": 0.0,
            "cw_samples": 0,
            "ccw_samples": 0,
            "commanded_omega": max_omega,
            "success": False,
            "error": str(exc),
        }


def _display_results(console: Console, result: Dict[str, Any]) -> None:
    """Display rotation calibration results."""
    if not result.get("success"):
        console.print(f"\n[red]Calibration failed: {result.get('error', 'Unknown error')}[/red]")
        return
    
    table = Table(title="Rotation Speed Calibration Results")
    table.add_column("Direction", style="cyan")
    table.add_column("Measured Speed (rad/s)", justify="right", style="green")
    table.add_column("Samples", justify="right", style="dim")
    
    table.add_row(
        "Clockwise",
        f"{result['cw_speed']:.3f}",
        str(result['cw_samples']),
    )
    table.add_row(
        "Counter-Clockwise",
        f"{result['ccw_speed']:.3f}",
        str(result['ccw_samples']),
    )
    
    # Average of both directions
    avg_speed = (result['cw_speed'] + result['ccw_speed']) / 2.0
    table.add_row(
        "[bold]Average[/bold]",
        f"[bold]{avg_speed:.3f}[/bold]",
        "[dim]—[/dim]",
    )
    
    console.print()
    console.print(table)
    
    # Show comparison to commanded speed
    commanded = result.get('commanded_omega', 0)
    if commanded > 0:
        ratio = (avg_speed / commanded) * 100
        console.print()
        console.print(f"[dim]Commanded: {commanded:.2f} rad/s[/dim]")
        console.print(f"[dim]Achieved: {ratio:.1f}% of commanded speed[/dim]")


def _update_rotation_speed(
    config: Dict[str, Any],
    cw_speed: float,
    ccw_speed: float,
) -> bool:
    """
    Update robot's max rotation speed in config.
    
    Saves to robot.drive.limits.max_rotation_speed
    Returns True if updated.
    """
    if "robot" not in config:
        return False
    
    if "drive" not in config["robot"]:
        return False
    
    if "limits" not in config["robot"]["drive"]:
        config["robot"]["drive"]["limits"] = {}
    
    limits = config["robot"]["drive"]["limits"]
    
    # Use average of both directions
    avg_speed = (cw_speed + ccw_speed) / 2.0
    
    # Round to 2 decimal places
    limits["max_rotation_speed"] = round(avg_speed, 2)
    
    return True


def _save_results_to_yaml(
    console: Console,
    project_root: Path,
    config: Dict[str, Any],
    result: Dict[str, Any],
    auto_save: bool = False,
) -> None:
    """Save rotation speed to project YAML."""
    if not result.get("success"):
        console.print("\n[yellow]No successful measurement to save.[/yellow]")
        return
    
    if not auto_save:
        avg_speed = (result['cw_speed'] + result['ccw_speed']) / 2.0
        console.print(f"\n[cyan]Save max rotation speed ({avg_speed:.2f} rad/s) to raccoon.project.yml?[/cyan]")
        if not click.confirm("Save calibration?", default=True):
            console.print("[yellow]Rotation speed calibration not saved.[/yellow]")
            return
    
    if not _update_rotation_speed(config, result['cw_speed'], result['ccw_speed']):
        console.print("[red]Failed to update configuration structure.[/red]")
        return
    
    try:
        save_project_config(config, project_root)
        avg_speed = (result['cw_speed'] + result['ccw_speed']) / 2.0
        console.print(f"\n[green]✓ Max rotation speed saved: {avg_speed:.2f} rad/s[/green]")
        console.print(f"[dim]  Location: robot.drive.limits.max_rotation_speed[/dim]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save configuration: {exc}[/red]")


def rotation_local(
    ctx: click.Context,
    project_root: Path,
    config: Dict[str, Any],
    duration: float,
    auto_save: bool = False,
) -> None:
    """Run rotation speed calibration locally (on the Pi itself)."""
    console: Console = ctx.obj["console"]
    
    # Add project root to Python path for imports
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    
    console.print(Panel(
        f"[bold cyan]Robot Rotation Speed Calibration[/bold cyan]\n\n"
        f"Test duration per direction: [yellow]{duration}s[/yellow]\n\n"
        f"[dim]This will make the robot spin in place at maximum speed.[/dim]\n"
        f"[yellow]⚠ Ensure the robot has clear space to rotate safely![/yellow]",
        border_style="cyan"
    ))
    
    # Collect data
    result = _collect_rotation_data(console, config, duration)
    
    # Display results
    _display_results(console, result)
    
    if not result.get("success"):
        raise SystemExit(1)
    
    # Save to YAML
    _save_results_to_yaml(console, project_root, config, result, auto_save=auto_save)


async def rotation_remote(
    ctx: click.Context,
    project_root: Path,
    config: Dict[str, Any],
    duration: float,
) -> None:
    """Run rotation speed calibration on the connected Pi."""
    console: Console = ctx.obj["console"]
    
    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.commands.sync_cmd import sync_project_interactive
    
    # Sync project first
    if not sync_project_interactive(project_root, console):
        console.print("[red]Cannot run calibration with unresolved conflicts[/red]")
        raise SystemExit(1)
    console.print()
    
    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)
    
    console.print(f"[cyan]Running rotation calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")
    
    # Build args for calibrate rotation command
    args = [
        "rotation",
        "--yes",
        "--duration", str(duration),
    ]
    
    # Start the calibrate command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start rotation calibration on Pi: {e}[/red]")
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
    else:
        console.print("[yellow]Warning: Failed to sync results. Run 'raccoon sync' manually.[/yellow]")
    
    if exit_code == 0:
        console.print()
        console.print("[green]Rotation calibration completed on Pi![/green]")
        return
    
    console.print()
    console.print(f"[red]Rotation calibration failed with exit code {exit_code}[/red]")
    raise SystemExit(exit_code)
