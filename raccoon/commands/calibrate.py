"""Calibrate command — encoder ticks (Phase 1) and autotune (Phase 2)."""

from __future__ import annotations

import asyncio
import logging
import math
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from raccoon.codegen import create_pipeline
from raccoon.project import ProjectError, load_project_config, require_project, save_project_keys

logger = logging.getLogger("raccoon")

NUM_TRIALS = 3

# Autotune entry script written temporarily to the project root.
# Imports Robot from src.hardware.robot and runs auto_tune() as a mission step.
_AUTOTUNE_SCRIPT = """\
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from libstp import auto_tune
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq
from src.hardware.robot import Robot


class _AutotuneMission(Mission):
    def sequence(self) -> Sequential:
        return seq([auto_tune()])


robot = Robot()
robot.missions = [_AutotuneMission()]
robot.start()
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_motor_defs(config: dict) -> Dict[str, tuple[int, bool]]:
    """Return {name: (port, inverted)} for every Motor in definitions."""
    result: Dict[str, tuple[int, bool]] = {}
    for name, defn in config.get("definitions", {}).items():
        if isinstance(defn, dict) and defn.get("type") == "Motor":
            result[name] = (int(defn.get("port", 0)), bool(defn.get("inverted", False)))
    return result


def _read_encoder_local(port: int, inverted: bool) -> int:
    from libstp.hal import Motor as HalMotor  # type: ignore

    return HalMotor(port=port, inverted=inverted).get_position()


def _make_remote_reader(api_client) -> Callable[[int, bool], int]:
    def _read(port: int, inverted: bool) -> int:
        async def _inner():
            async with api_client:
                reading = await api_client.read_encoder(port, inverted)
                if not reading.success:
                    raise RuntimeError(f"Failed to read encoder: {reading.error}")
                return reading.position

        return asyncio.run(_inner())

    return _read


def _calibrate_single_motor(
    console: Console,
    motor_name: str,
    port: int,
    inverted: bool,
    read_fn: Callable[[int, bool], int],
) -> float:
    measurements = []
    for trial in range(1, NUM_TRIALS + 1):
        console.print(f"\n[bold cyan]Trial {trial}/{NUM_TRIALS} — {motor_name}[/bold cyan]")
        start = read_fn(port, inverted)
        console.print(f"[dim]Encoder position: {start}[/dim]")
        console.print("[green]→ Rotate the wheel exactly ONE full turn (360°), then press Enter.[/green]")
        click.prompt("", default="", show_default=False, prompt_suffix="")
        end = read_fn(port, inverted)
        ticks = abs(end - start)
        console.print(f"[cyan]  Measured: {ticks} ticks[/cyan]")
        measurements.append(ticks)

    avg = sum(measurements) / len(measurements)
    console.print(f"[bold green]{motor_name} average: {avg:.1f} ticks/rev[/bold green]")
    return avg


# ---------------------------------------------------------------------------
# Phase 1 — Encoder ticks
# ---------------------------------------------------------------------------


def _phase1(console: Console, project_root: Path, config: dict, local: bool) -> None:
    console.print(Panel("[bold]Phase 1 — Encoder Ticks Calibration[/bold]", border_style="cyan"))

    motor_defs = _get_motor_defs(config)
    if not motor_defs:
        console.print("[yellow]No motors found in definitions — skipping Phase 1.[/yellow]")
        return

    read_fn: Callable[[int, bool], int]
    if local:
        read_fn = _read_encoder_local
    else:
        try:
            from raccoon.client.connection import get_connection_manager
            from raccoon.client.api import create_api_client

            manager = get_connection_manager()
            state = manager.state
            api_client = create_api_client(state.pi_address, state.pi_port, api_token=state.api_token)
            read_fn = _make_remote_reader(api_client)
        except Exception as exc:
            console.print(f"[yellow]Cannot reach Pi for encoder reads ({exc}). Using local HAL.[/yellow]")
            read_fn = _read_encoder_local

    results: Dict[str, float] = {}
    for motor_name, (port, inverted) in motor_defs.items():
        console.print(f"\n[bold]Motor: {motor_name} (port {port})[/bold]")
        console.print("[yellow]Make sure the wheel can spin freely.[/yellow]")
        if not click.confirm(f"Calibrate {motor_name}?", default=True):
            console.print(f"[dim]Skipping {motor_name}[/dim]")
            continue
        try:
            avg = _calibrate_single_motor(console, motor_name, port, inverted, read_fn)
            results[motor_name] = avg
        except Exception as exc:
            console.print(f"[red]Error calibrating {motor_name}: {exc}[/red]")

    if not results:
        console.print("[yellow]No motors calibrated.[/yellow]")
        return

    table = Table(title="Encoder Ticks Summary")
    table.add_column("Motor", style="cyan")
    table.add_column("Ticks/Rev", justify="right")
    table.add_column("Rad/Tick", justify="right")
    for name, ticks in results.items():
        rad_per_tick = (2 * math.pi) / ticks if ticks > 0 else 0.0
        table.add_row(name, f"{ticks:.1f}", f"{rad_per_tick:.7f}")
    console.print(table)

    definitions = config.setdefault("definitions", {})
    for name, ticks in results.items():
        if name in definitions and ticks > 0:
            definitions[name].setdefault("calibration", {})["ticks_to_rad"] = round(
                (2 * math.pi) / ticks, 7
            )
    save_project_keys(project_root, {"definitions": definitions})
    console.print("[green]Saved ticks_to_rad to raccoon.project.yml[/green]")


# ---------------------------------------------------------------------------
# Phase 2 — Autotune
# ---------------------------------------------------------------------------


def _phase2_local(console: Console, project_root: Path, config: dict) -> None:
    console.print(Panel("[bold]Phase 2 — Autotune[/bold]", border_style="cyan"))

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    pipeline = create_pipeline()
    pipeline.run_all(config, project_root / "src" / "hardware", format_code=True)

    script_path = project_root / "_raccoon_autotune.py"
    script_path.write_text(_AUTOTUNE_SCRIPT)

    try:
        console.print("[cyan]Running auto_tune() as a mission step...[/cyan]")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")
        proc = subprocess.Popen([sys.executable, str(script_path)], cwd=project_root)
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            console.print("\n[yellow]Ctrl+C — stopping autotune...[/yellow]")
            proc.terminate()
            try:
                returncode = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()

        style = "bold green" if returncode == 0 else "bold red"
        console.print(Panel.fit(f"Autotune exited with code {returncode}", style=style))
        if returncode != 0:
            raise SystemExit(returncode)
    finally:
        script_path.unlink(missing_ok=True)


async def _phase2_remote(ctx: click.Context, project_root: Path, config: dict) -> None:
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.client.sftp_sync import SyncDirection
    from raccoon.commands.sync_cmd import sync_project_interactive

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    pipeline = create_pipeline()
    pipeline.run_all(config, project_root / "src" / "hardware", format_code=True)

    if not sync_project_interactive(project_root, console):
        console.print("[red]Sync failed — cannot run autotune remotely.[/red]")
        raise SystemExit(1)
    console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")

    console.print(f"[cyan]Running auto_tune on {state.pi_hostname}...[/cyan]")

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            # Pi will run: raccoon calibrate --skip-ticks --local
            result = await client.calibrate_project(project_uuid, args=["--skip-ticks"])
        except Exception as exc:
            console.print(f"[red]Failed to start autotune on Pi: {exc}[/red]")
            raise SystemExit(1)

        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

        cancel_requested = False

        def _sig(sig, frame):
            nonlocal cancel_requested
            if not cancel_requested:
                cancel_requested = True
                console.print("\n[yellow]Cancelling...[/yellow]")
                handler.cancel()

        original = signal.signal(signal.SIGINT, _sig)
        try:
            final_status = handler.stream_to_console(console)
        finally:
            signal.signal(signal.SIGINT, original)

        console.print()
        sync_project_interactive(project_root, console, direction=SyncDirection.PULL, update=True)

        exit_code = final_status.get("exit_code", -1)
        status_str = final_status.get("status", "unknown")
        style = "bold green" if exit_code == 0 else "bold red"
        console.print(
            Panel.fit(f"Remote autotune {status_str} with code {exit_code}", style=style)
        )
        if exit_code != 0:
            raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command(name="calibrate")
@click.option("--local", "-l", is_flag=True, help="Run locally on this machine (requires hardware)")
@click.option("--skip-ticks", is_flag=True, help="Skip Phase 1 (encoder ticks)")
@click.option("--skip-autotune", is_flag=True, help="Skip Phase 2 (autotune)")
@click.pass_context
def calibrate_command(
    ctx: click.Context, local: bool, skip_ticks: bool, skip_autotune: bool
) -> None:
    """Two-phase robot calibration.

    \b
    Phase 1: Encoder ticks — rotate each wheel by hand to measure ticks/revolution.
    Phase 2: Autotune — runs auto_tune() as a mission step via the robot context.

    By default, Phase 1 reads encoders from the connected Pi and Phase 2 runs
    on the Pi.  Use --local to run everything on this machine instead.
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        config = load_project_config(project_root)
    except ProjectError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise SystemExit(1) from exc

    if not local:
        try:
            from raccoon.client.connection import (
                get_connection_manager,
                ParamikoVersionError,
                print_paramiko_version_error,
            )

            manager = get_connection_manager()
            if not manager.is_connected:
                project_conn = manager.load_from_project(project_root)
                if project_conn and project_conn.pi_address:
                    manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
                else:
                    known_pis = manager.load_known_pis()
                    if known_pis:
                        pi = known_pis[0]
                        manager.connect_sync(pi.get("address"), pi.get("port", 8421))
        except Exception as exc:
            console.print(f"[yellow]No Pi connection ({exc}). Running locally.[/yellow]")
            local = True

    if not skip_ticks:
        _phase1(console, project_root, config, local)
        config = load_project_config(project_root)

    if not skip_autotune:
        if local:
            _phase2_local(console, project_root, config)
        else:
            asyncio.run(_phase2_remote(ctx, project_root, config))
