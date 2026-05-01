"""Calibrate command — encoder ticks, autotune and servo calibration."""

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
import questionary
from questionary import Style as QStyle

from raccoon_cli.codegen import create_pipeline
from raccoon_cli.project import ProjectError, load_project_config, require_project, save_project_keys

logger = logging.getLogger("raccoon")

# Style for servo calibration questions
_STYLE = QStyle([
    ("qmark",       "fg:#8b5cf6 bold"),
    ("question",    "bold"),
    ("answer",      "fg:#22d3ee bold"),
    ("pointer",     "fg:#8b5cf6 bold"),
    ("highlighted", "fg:#8b5cf6 bold"),
    ("selected",    "fg:#22d3ee"),
    ("separator",   "fg:#6b7280"),
    ("instruction", "fg:#6b7280 italic"),
])

NUM_TRIALS = 3

# Autotune entry script written temporarily to the project root.
# Imports Robot from src.hardware.robot and runs auto_tune() as a mission step.
_AUTOTUNE_SCRIPT = """\
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raccoon import auto_tune
from raccoon.mission.api import Mission
from raccoon.step.sequential import Sequential, seq
from src.hardware.robot import Robot


class _AutotuneMission(Mission):
    def sequence(self) -> Sequential:
        return seq([auto_tune()])


robot = Robot()
robot.missions = [_AutotuneMission()]
robot.start()
"""


# ---------------------------------------------------------------------------
# Calibrate Encoder ticks
# ---------------------------------------------------------------------------

def _calibrate_ticks(console: Console, project_root: Path, config: dict, local: bool) -> None:
    console.print(Panel("[bold]Encoder Ticks Calibration[/bold]", border_style="cyan"))

    motor_defs = _get_motor_defs(config)
    if not motor_defs:
        console.print("[yellow]No motors found in definitions — aborting Tick Calibration.[/yellow]")
        return

    read_fn: Callable[[int, bool], int]
    if local:
        read_fn = _read_encoder_local
    else:
        try:
            from raccoon_cli.client.connection import get_connection_manager
            from raccoon_cli.client.api import create_api_client

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

# Calibrate Encoder Ticks Helpers

def _get_motor_defs(config: dict) -> Dict[str, tuple[int, bool]]:
    """Return {name: (port, inverted)} for every Motor in definitions."""
    result: Dict[str, tuple[int, bool]] = {}
    for name, defn in config.get("definitions", {}).items():
        if isinstance(defn, dict) and defn.get("type") == "Motor":
            result[name] = (int(defn.get("port", 0)), bool(defn.get("inverted", False)))
    return result


def _read_encoder_local(port: int, inverted: bool) -> int:
    from raccoon.hal import Motor as HalMotor  # type: ignore

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
# Autotune
# ---------------------------------------------------------------------------

def _autotune_local(console: Console, project_root: Path, config: dict) -> None:
    console.print(Panel("[bold]Autotune Calibration[/bold]", border_style="cyan"))

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


async def _autotune_remote(ctx: click.Context, project_root: Path, config: dict) -> None:
    console: Console = ctx.obj["console"]

    from raccoon_cli.client.connection import get_connection_manager
    from raccoon_cli.client.api import create_api_client
    from raccoon_cli.client.output_handler import OutputHandler
    from raccoon_cli.client.sftp_sync import SyncDirection
    from raccoon_cli.commands.sync_cmd import sync_project_interactive

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
# Calibrate Servos
# ---------------------------------------------------------------------------

def _calibrate_servos(ctx: click.Context, project_root: Path, config: dict) -> None:
    import httpx
    import tty
    import termios

    console: Console = ctx.obj["console"]

    console.print(Panel("[bold]Servo Calibration[/bold]", border_style="cyan"))

    # Filter to servos that have named positions
    servo_defs = {
        name: defn
        for name, defn in config.get("definitions", {}).items()
        if isinstance(defn, dict)
           and defn.get("type") == "Servo"
           and defn.get("positions")
    }
    if not servo_defs:
        console.print("[yellow]No servos with named positions found — aborting.[/yellow]")
        return

    from raccoon_cli.client.connection import get_connection_manager
    manager = get_connection_manager()
    state = manager.state
    base_url = f"http://{state.pi_address}:{state.pi_port}/api/v1"

    saved: Dict[str, tuple[str, float]] = {}  # servo_name → (pos_name, final_deg)

    while True:
        servo_pick = _pick_servo(servo_defs)
        if servo_pick is None:
            break
        servo_name, defn = servo_pick

        pos_pick = _pick_position(servo_name, defn)
        if pos_pick is None:
            continue
        pos_name, start_deg = pos_pick

        port = int(defn.get("port", 0))

        # Start session
        try:
            r = httpx.post(
                f"{base_url}/calibrate-servo/start",
                params={"servo_id": servo_name, "port": port, "initial_angle": start_deg},
                timeout=5.0,
            )
            if r.status_code == 409:
                console.print("[red]A calibration session is already active on the Pi.[/red]")
                continue
            r.raise_for_status()
        except Exception as exc:
            console.print(f"[red]Could not start session: {exc}[/red]")
            continue

        console.print(f"\n[bold cyan]Jogging {servo_name} · {pos_name}[/bold cyan]  [dim](starting at {start_deg:.1f}°)[/dim]")
        console.print("  [dim]↑ / k  +0.5°    ↓ / j  −0.5°    Enter  confirm    Q  skip[/dim]\n")

        current_deg = start_deg
        skipped = False

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    if seq == "[A":       delta = +0.5   # ↑
                    elif seq == "[B":     delta = -0.5   # ↓
                    else:                 continue
                elif ch in ("k", "K"):   delta = +0.5
                elif ch in ("j", "J"):   delta = -0.5
                elif ch in ("\r", "\n"): break
                elif ch in ("q", "Q"):
                    skipped = True
                    break
                else:
                    continue

                try:
                    httpx.post(
                        f"{base_url}/calibrate-servo/move",
                        params={"angle": delta},
                        timeout=2.0,
                    )
                    current_deg = max(0.0, min(270.0, current_deg + delta))
                except Exception:
                    pass  # failed nudge — keep going, display is still accurate

                sys.stdout.write(f"\r  Position: {current_deg:+.1f}°  ")
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            sys.stdout.write("\n")
            sys.stdout.flush()

        # End session — always, even if skipped or something threw
        final_deg = current_deg
        try:
            r = httpx.post(f"{base_url}/calibrate-servo/end", timeout=5.0)
            if r.status_code == 200:
                final_deg = r.json().get("final_deg", current_deg)
        except Exception as exc:
            console.print(f"[yellow]Could not cleanly end session: {exc}[/yellow]")

        if skipped:
            console.print(f"[dim]Skipped {servo_name} · {pos_name}[/dim]")
        else:
            saved[servo_name] = (pos_name, final_deg)
            console.print(f"[green]{servo_name}.{pos_name} = {final_deg:.1f}°[/green]")

        if not questionary.confirm("Calibrate another servo?", default=True, style=_STYLE).ask():
            break

    if not saved:
        return

    # Write back only the changed position values
    definitions = config.setdefault("definitions", {})
    for servo_name, (pos_name, new_deg) in saved.items():
        definitions[servo_name]["positions"][pos_name] = round(new_deg, 1)
    save_project_keys(project_root, {"definitions": definitions})

    table = Table(title="Saved Servo Positions")
    table.add_column("Servo", style="cyan")
    table.add_column("Position")
    table.add_column("Value", justify="right")
    for servo_name, (pos_name, deg) in saved.items():
        table.add_row(servo_name, pos_name, f"{deg:.1f}°")
    console.print(table)
    console.print("[green]Saved to raccoon.project.yml[/green]")

# Calibrate Servos Helpers

def _pick_servo(servo_defs: Dict[str, dict]) -> Optional[tuple[str, dict]]:
    choices = [
        questionary.Choice(
            title=f"{name}  ({', '.join(defn['positions'].keys())})",
            value=name,
        )
        for name, defn in servo_defs.items()
    ]
    choices.append(questionary.Choice(title="✕  Done", value=None))
    result = questionary.select("Select servo to calibrate:", choices=choices, style=_STYLE).ask()
    if result is None:
        return None
    return result, servo_defs[result]


def _pick_position(servo_name: str, defn: dict) -> Optional[tuple[str, float]]:
    positions: dict = defn["positions"]
    choices = [
        questionary.Choice(title=f"{pos_name}  ({deg}°)", value=pos_name)
        for pos_name, deg in positions.items()
    ]
    choices.append(questionary.Choice(title="✕  Cancel", value=None))
    result = questionary.select(
        f"Select position to calibrate on {servo_name}:", choices=choices, style=_STYLE
    ).ask()
    if result is None:
        return None
    return result, float(positions[result])


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

@click.group(name="calibrate", invoke_without_command=True)
@click.option("--local", "-l", is_flag=True, help="Run locally on this machine (requires hardware)")
@click.pass_context
def calibrate_group(
        ctx: click.Context, local: bool
) -> None:
    """Robot calibration.

    \b
    Runs all phases by default:
      Phase 1 (ticks)    — rotate each wheel to measure ticks/revolution.
      Phase 2 (autotune) — runs auto_tune() as a mission step.
      Phase 3 (servos)   — sweeps servos to find min/max/center.

    Or run a single phase with a subcommand:
      calibrate ticks
      calibrate autotune
      calibrate servos
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
            from raccoon_cli.client.connection import (
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

    ctx.obj["local"] = local
    ctx.obj["project_root"] = project_root
    ctx.obj["config"] = config

    if ctx.invoked_subcommand is not None:
        return

    _calibrate_ticks(console, project_root, config, local)
    if local:
        _autotune_local(console, project_root, config)
    else:
        asyncio.run(_autotune_remote(ctx, project_root, config))
    _calibrate_servos(ctx, project_root, config)


@calibrate_group.command(name="ticks")
@click.pass_context
def calibrate_ticks_cmd(ctx: click.Context) -> None:
    """Phase 1: measure encoder ticks/revolution."""
    console = ctx.obj["console"]
    _calibrate_ticks(console, ctx.obj["project_root"], ctx.obj["config"], ctx.obj["local"])


@calibrate_group.command(name="autotune")
@click.pass_context
def calibrate_autotune_cmd(ctx: click.Context) -> None:
    """Phase 2: run auto_tune() as a mission step."""
    console = ctx.obj["console"]
    if ctx.obj["local"]:
        _autotune_local(console, ctx.obj["project_root"], ctx.obj["config"])
    else:
        asyncio.run(_autotune_remote(ctx, ctx.obj["project_root"], ctx.obj["config"]))


@calibrate_group.command(name="servos")
@click.pass_context
def calibrate_servos_cmd(ctx: click.Context) -> None:
    """Phase 3: jog servos to find named positions."""
    _calibrate_servos(ctx, ctx.obj["project_root"], ctx.obj["config"])