"""Max speed calibration for motors."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .utils import save_project_config

logger = logging.getLogger("raccoon")


def _test_motor_speed(
    console: Console,
    motor,
    power: int,
    duration: float,
    direction_name: str,
) -> float:
    """
    Test motor at specified power level for given duration.
    
    Returns average speed in ticks/second.
    """
    console.print(f"  Testing {direction_name} at {power}% power for {duration}s...")
    
    # Set motor power
    motor.set_speed(power)
    
    # Record start position
    start_pos = motor.get_position()
    start_time = time.time()
    
    # Wait for duration
    time.sleep(duration)
    
    # Record end position
    end_pos = motor.get_position()
    end_time = time.time()
    
    # Stop motor
    motor.set_speed(0)
    
    # Calculate average speed (absolute value for reverse)
    elapsed = end_time - start_time
    position_delta = abs(end_pos - start_pos)
    avg_speed_ticks = position_delta / elapsed if elapsed > 0 else 0
    
    return avg_speed_ticks


def _collect_maxspeed_data(
    console: Console,
    config: Dict[str, Any],
    duration: float,
) -> List[Dict[str, Any]]:
    """
    Collect max speed data for all motors in the project.
    
    Returns list of results for each motor.
    """
    # Import hardware library
    try:
        from libstp.hal import Motor
    except ImportError as exc:
        console.print(f"[red]Failed to import libstp: {exc}[/red]")
        console.print("[yellow]Make sure libstp is installed and you're running on the Pi.[/yellow]")
        raise SystemExit(1) from exc
    
    # Find all motors in definitions
    definitions = config.get("definitions", {})
    motors_to_test = []
    
    for name, definition in definitions.items():
        if definition.get("type") == "Motor":
            port = definition.get("port")
            inverted = definition.get("inverted", False)
            ticks_to_rad = definition.get("calibration", {}).get("ticks_to_rad", 1.0)
            
            if port is not None:
                motors_to_test.append({
                    "name": name,
                    "port": port,
                    "inverted": inverted,
                    "ticks_to_rad": ticks_to_rad,
                })
    
    if not motors_to_test:
        console.print("[yellow]No motors found in project configuration.[/yellow]")
        return []
    
    console.print(f"\n[cyan]Found {len(motors_to_test)} motor(s) to test[/cyan]")
    
    results = []
    
    # Create progress display
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Testing motors...", total=len(motors_to_test) * 2)
        
        for motor_info in motors_to_test:
            name = motor_info["name"]
            port = motor_info["port"]
            inverted = motor_info["inverted"]
            ticks_to_rad = motor_info["ticks_to_rad"]
            
            console.print(f"\n[bold cyan]Testing {name} (port {port})[/bold cyan]")
            
            try:
                # Initialize motor
                motor = Motor(port=port, inverted=inverted)
                
                # Test forward direction
                progress.update(task, description=f"Testing {name} forward...")
                forward_speed_ticks = _test_motor_speed(
                    console, motor, 100, duration, "forward"
                )
                progress.advance(task)
                
                # Brief pause between tests
                time.sleep(0.5)
                
                # Test reverse direction
                progress.update(task, description=f"Testing {name} reverse...")
                reverse_speed_ticks = _test_motor_speed(
                    console, motor, -100, duration, "reverse"
                )
                progress.advance(task)
                
                # Convert to rad/s
                forward_speed_rad = forward_speed_ticks * ticks_to_rad
                reverse_speed_rad = reverse_speed_ticks * ticks_to_rad
                
                results.append({
                    "name": name,
                    "port": port,
                    "forward_speed_ticks": forward_speed_ticks,
                    "reverse_speed_ticks": reverse_speed_ticks,
                    "forward_speed_rad": forward_speed_rad,
                    "reverse_speed_rad": reverse_speed_rad,
                    "ticks_to_rad": ticks_to_rad,
                    "success": True,
                })
                
                console.print(f"  [green]✓ Forward: {forward_speed_rad:.2f} rad/s[/green]")
                console.print(f"  [green]✓ Reverse: {reverse_speed_rad:.2f} rad/s[/green]")
                
            except Exception as exc:
                console.print(f"  [red]✗ Failed: {exc}[/red]")
                results.append({
                    "name": name,
                    "port": port,
                    "forward_speed_ticks": 0,
                    "reverse_speed_ticks": 0,
                    "forward_speed_rad": 0,
                    "reverse_speed_rad": 0,
                    "ticks_to_rad": ticks_to_rad,
                    "success": False,
                    "error": str(exc),
                })
                progress.advance(task, advance=2)
            finally:
                # Ensure motor is stopped
                try:
                    motor.set_speed(0)
                except Exception:
                    pass
    
    return results


def _display_results(console: Console, results: List[Dict[str, Any]]) -> None:
    """Display max speed results in a table."""
    if not results:
        return
    
    table = Table(title="Max Speed Calibration Results")
    table.add_column("Motor", style="cyan")
    table.add_column("Port", justify="right")
    table.add_column("Forward (rad/s)", justify="right", style="green")
    table.add_column("Reverse (rad/s)", justify="right", style="green")
    table.add_column("Forward (ticks/s)", justify="right", style="dim")
    table.add_column("Reverse (ticks/s)", justify="right", style="dim")
    table.add_column("Status", justify="center")
    
    for result in results:
        status = "[green]✓[/green]" if result.get("success") else "[red]✗[/red]"
        
        table.add_row(
            result["name"],
            str(result["port"]),
            f"{result['forward_speed_rad']:.2f}",
            f"{result['reverse_speed_rad']:.2f}",
            f"{result['forward_speed_ticks']:.1f}",
            f"{result['reverse_speed_ticks']:.1f}",
            status,
        )
    
    console.print()
    console.print(table)


def _update_motor_maxspeed(
    config: Dict[str, Any],
    motor_name: str,
    forward_speed_rad: float,
    reverse_speed_rad: float,
) -> bool:
    """Update motor's max speed calibration in config. Returns True if updated."""
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
    
    # Update max speed values
    calibration["max_forward_speed"] = round(forward_speed_rad, 2)
    calibration["max_reverse_speed"] = round(reverse_speed_rad, 2)
    
    return True


def _save_results_to_yaml(
    console: Console,
    project_root: Path,
    config: Dict[str, Any],
    results: List[Dict[str, Any]],
    auto_save: bool = False,
) -> None:
    """Save max speed results to project YAML."""
    if not results:
        return
    
    # Filter successful results
    successful_results = [r for r in results if r.get("success")]
    
    if not successful_results:
        console.print("\n[yellow]No successful measurements to save.[/yellow]")
        return
    
    if not auto_save:
        console.print(f"\n[cyan]Save max speed calibration for {len(successful_results)} motor(s) to raccoon.project.yml?[/cyan]")
        if not click.confirm("Save calibration?", default=True):
            console.print("[yellow]Max speed calibration not saved.[/yellow]")
            return
    
    # Update config for each motor
    updated_count = 0
    for result in successful_results:
        if _update_motor_maxspeed(
            config,
            result["name"],
            result["forward_speed_rad"],
            result["reverse_speed_rad"],
        ):
            updated_count += 1
    
    if updated_count == 0:
        console.print("[yellow]No motors were updated in configuration.[/yellow]")
        return
    
    # Save to file
    try:
        save_project_config(config, project_root)
        console.print(f"\n[green]✓ Max speed calibration saved for {updated_count} motor(s)[/green]")
        
        # Show what was saved
        for result in successful_results:
            console.print(f"[dim]  {result['name']}: "
                         f"forward={result['forward_speed_rad']:.2f} rad/s, "
                         f"reverse={result['reverse_speed_rad']:.2f} rad/s[/dim]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save configuration: {exc}[/red]")


def maxspeed_local(
    ctx: click.Context,
    project_root: Path,
    config: Dict[str, Any],
    duration: float,
    auto_save: bool = False,
) -> None:
    """Run max speed calibration locally (on the Pi itself)."""
    console: Console = ctx.obj["console"]
    
    console.print(Panel(
        f"[bold cyan]Max Speed Calibration[/bold cyan]\n\n"
        f"Test duration per direction: [yellow]{duration}s[/yellow]\n"
        f"Power levels: [yellow]±100%[/yellow]\n\n"
        f"[dim]This will test all motors at full power in both directions.[/dim]",
        border_style="cyan"
    ))
    
    # Collect data
    results = _collect_maxspeed_data(console, config, duration)
    
    if not results:
        console.print("\n[red]No results collected.[/red]")
        raise SystemExit(1)
    
    # Display results
    _display_results(console, results)
    
    # Save to YAML
    _save_results_to_yaml(console, project_root, config, results, auto_save=auto_save)


async def maxspeed_remote(
    ctx: click.Context,
    project_root: Path,
    config: Dict[str, Any],
    duration: float,
) -> None:
    """Run max speed calibration on the connected Pi."""
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
    
    console.print(f"[cyan]Running max speed calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")
    
    # Build args for calibrate maxspeed command
    args = [
        "maxspeed",
        "--yes",
        "--duration", str(duration),
    ]
    
    # Start the calibrate command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start max speed calibration on Pi: {e}[/red]")
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
        console.print("[green]Max speed calibration completed on Pi![/green]")
        return
    
    console.print()
    console.print(f"[red]Max speed calibration failed with exit code {exit_code}[/red]")
    raise SystemExit(exit_code)
