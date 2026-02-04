"""Interactive deadzone calibration using human observation.

BEMF is unreliable at low RPM, so we use human observation to find the
exact motor percentage where the wheel starts turning.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from .utils import find_motor_by_port, save_project_config

logger = logging.getLogger("raccoon")


@dataclass
class DeadzoneResult:
    """Result of deadzone calibration for a single motor."""
    motor_port: int
    motor_name: str
    start_percent_forward: int
    start_percent_reverse: int
    release_percent: int  # Typically ~70% of start


def _find_deadzone_interactive(
    console: Console,
    motor,
    direction: int,  # 1 for forward, -1 for reverse
    start_percent: int = 1,
    max_percent: int = 30,
    settle_time: float = 0.3,
) -> int:
    """
    Find the minimum percent that makes the motor turn using human observation.

    Returns the first percentage where user confirms movement.
    """
    direction_name = "FORWARD" if direction > 0 else "REVERSE"

    for percent in range(start_percent, max_percent + 1):
        # Apply speed
        motor.set_speed(percent * direction)
        time.sleep(settle_time)

        # Ask user - use simple y/n prompt
        console.print(f"\n[bold cyan]{percent}%[/bold cyan] ({direction_name})")
        is_turning = Confirm.ask("Is the wheel turning?", default=False)

        if is_turning:
            motor.brake()
            return percent

    # Hit max without movement
    motor.brake()
    console.print(f"[yellow]Warning: Motor didn't turn even at {max_percent}%[/yellow]")
    return max_percent


def _update_motor_deadzone(
    config: Dict[str, Any],
    motor_name: str,
    result: DeadzoneResult,
) -> bool:
    """
    Update motor's deadzone calibration in config.

    IMPORTANT: Only updates deadzone fields, preserves all other calibration.
    """
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

    # Only update deadzone fields - preserve everything else!
    calibration["deadzone"] = {
        "enable": True,
        "zero_window_percent": calibration.get("deadzone", {}).get("zero_window_percent", 2.0),
        "start_percent": float(max(result.start_percent_forward, result.start_percent_reverse)),
        "release_percent": float(result.release_percent),
    }

    return True


def calibrate_deadzone_local(
    ctx: click.Context,
    project_root: Path,
    config: Dict[str, Any],
    motor_ports: Optional[List[int]] = None,
    start_percent: int = 1,
    max_percent: int = 30,
    settle_time: float = 0.3,
    auto_save: bool = False,
) -> None:
    """Run interactive deadzone calibration locally (on the Pi itself)."""
    console: Console = ctx.obj["console"]

    # Import required libraries
    try:
        from libstp.hal import Motor
    except ImportError as exc:
        console.print(f"[red]Failed to import libstp: {exc}[/red]")
        console.print("[yellow]Make sure libstp is installed and you're running on the Pi.[/yellow]")
        raise SystemExit(1) from exc

    # Find motors to calibrate
    definitions = config.get("definitions", {})
    motors_to_calibrate: List[tuple[str, int]] = []

    if motor_ports:
        # Calibrate specific ports
        for port in motor_ports:
            motor_name = find_motor_by_port(config, port)
            if motor_name:
                motors_to_calibrate.append((motor_name, port))
            else:
                console.print(f"[yellow]Warning: No motor definition found for port {port}[/yellow]")
    else:
        # Find all motors in config
        for name, definition in definitions.items():
            if definition.get("type") == "Motor":
                port = definition.get("port")
                if port is not None:
                    motors_to_calibrate.append((name, port))

    if not motors_to_calibrate:
        console.print("[red]No motors found to calibrate.[/red]")
        console.print("[yellow]Define motors in raccoon.project.yml first.[/yellow]")
        raise SystemExit(1)

    console.print(Panel(
        f"[bold cyan]Interactive Deadzone Calibration[/bold cyan]\n\n"
        f"Motors: [yellow]{', '.join(name for name, _ in motors_to_calibrate)}[/yellow]\n"
        f"Range: [yellow]{start_percent}% to {max_percent}%[/yellow]\n"
        f"Settle time: [yellow]{settle_time}s[/yellow]\n\n"
        f"[dim]Watch the wheel and answer whether it's turning.[/dim]\n"
        f"[dim]BEMF is unreliable at low RPM, so human observation is more accurate.[/dim]",
        border_style="cyan"
    ))

    results: List[DeadzoneResult] = []

    for motor_name, port in motors_to_calibrate:
        console.print(f"\n[bold]{'='*50}[/bold]")
        console.print(f"[bold cyan]Calibrating: {motor_name} (port {port})[/bold cyan]")
        console.print(f"[bold]{'='*50}[/bold]")

        # Initialize motor
        try:
            motor_def = definitions[motor_name]
            inverted = motor_def.get("inverted", False)
            motor = Motor(port=port, inverted=inverted)
        except Exception as exc:
            console.print(f"[red]Failed to initialize motor {motor_name}: {exc}[/red]")
            continue

        try:
            console.print("\n[yellow]Watch the wheel carefully...[/yellow]")

            # Test forward direction
            console.print("\n[bold]Testing FORWARD direction:[/bold]")
            start_fwd = _find_deadzone_interactive(
                console, motor, direction=1,
                start_percent=start_percent, max_percent=max_percent,
                settle_time=settle_time,
            )
            console.print(f"[green]Forward start: {start_fwd}%[/green]")

            time.sleep(0.5)  # Brief pause between directions

            # Test reverse direction
            console.print("\n[bold]Testing REVERSE direction:[/bold]")
            start_rev = _find_deadzone_interactive(
                console, motor, direction=-1,
                start_percent=start_percent, max_percent=max_percent,
                settle_time=settle_time,
            )
            console.print(f"[green]Reverse start: {start_rev}%[/green]")

        finally:
            motor.brake()

        # Calculate release percent (~70% of start, hysteresis)
        avg_start = (start_fwd + start_rev) / 2
        release = max(1, int(avg_start * 0.7))

        result = DeadzoneResult(
            motor_port=port,
            motor_name=motor_name,
            start_percent_forward=start_fwd,
            start_percent_reverse=start_rev,
            release_percent=release,
        )
        results.append(result)

        console.print(f"\n[cyan]Result for {motor_name}:[/cyan]")
        console.print(f"  Forward: {start_fwd}%")
        console.print(f"  Reverse: {start_rev}%")
        console.print(f"  Release: {release}% (70% of avg)")

    if not results:
        console.print("[red]No calibration results collected.[/red]")
        raise SystemExit(1)

    # Display summary
    console.print("\n")
    _display_results_table(console, results)

    # Update configuration
    for result in results:
        _update_motor_deadzone(config, result.motor_name, result)

    # Save configuration
    if not auto_save:
        if not Confirm.ask("\nSave deadzone calibration to raccoon.project.yml?", default=True):
            console.print("[yellow]Deadzone calibration not saved.[/yellow]")
            return

    try:
        save_project_config(config, project_root)
        console.print(f"\n[green]✓ Deadzone calibration saved![/green]")
        console.print("[dim]Other calibration values (PID, FF, BEMF) were preserved.[/dim]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save configuration: {exc}[/red]")
        raise SystemExit(1) from exc


def _display_results_table(console: Console, results: List[DeadzoneResult]) -> None:
    """Display calibration results in a formatted table."""
    table = Table(title="Deadzone Calibration Results", expand=True)
    table.add_column("Motor", style="bold cyan")
    table.add_column("Port", justify="right")
    table.add_column("Forward", justify="right", style="green")
    table.add_column("Reverse", justify="right", style="green")
    table.add_column("Start %", justify="right", style="yellow")
    table.add_column("Release %", justify="right", style="dim")

    for r in results:
        start = max(r.start_percent_forward, r.start_percent_reverse)
        table.add_row(
            r.motor_name,
            str(r.motor_port),
            f"{r.start_percent_forward}%",
            f"{r.start_percent_reverse}%",
            f"{start}%",
            f"{r.release_percent}%",
        )

    console.print(Panel(table, border_style="green"))


async def calibrate_deadzone_remote(
    ctx: click.Context,
    project_root: Path,
    config: Dict[str, Any],
    motor_ports: Optional[List[int]] = None,
    start_percent: int = 1,
    max_percent: int = 30,
    settle_time: float = 0.3,
) -> None:
    """Run interactive deadzone calibration on the connected Pi."""
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

    console.print(f"[cyan]Running deadzone calibration for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Build args
    args = ["deadzone", "--yes"]
    if motor_ports:
        for port in motor_ports:
            args.extend(["--motor-port", str(port)])
    args.extend(["--start-percent", str(start_percent)])
    args.extend(["--max-percent", str(max_percent)])
    args.extend(["--settle-time", str(settle_time)])

    # Start command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start deadzone calibration on Pi: {e}[/red]")
            raise SystemExit(1)

        # Stream output
        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print()
        console.print("[yellow]Note: You'll need to answer prompts on the Pi terminal.[/yellow]")
        console.print()

        final_status = handler.stream_to_console(console)
        exit_code = final_status.get("exit_code", -1)

    # Sync results back
    console.print()
    console.print("[dim]Syncing calibration results...[/dim]")
    if sync_project_interactive(project_root, console):
        console.print("[green]✓ Calibration results synced to local project[/green]")
    else:
        console.print("[yellow]Warning: Failed to sync results. Run 'raccoon sync' manually.[/yellow]")

    if exit_code == 0:
        console.print()
        console.print("[green]Deadzone calibration completed![/green]")
        return

    console.print()
    console.print(f"[red]Deadzone calibration failed with exit code {exit_code}[/red]")
    raise SystemExit(exit_code)
