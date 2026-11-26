"""Calibrate command — encoder ticks, autotune and servo calibration."""

from __future__ import annotations

import asyncio
import csv
import logging
import math
import queue
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import questionary
from questionary import Style as QStyle

from raccoon_cli.codegen import create_pipeline
from raccoon_cli.project import (
    ProjectError,
    load_project_config,
    require_project,
)
from raccoon_cli.yaml_utils import load_yaml_raw, save_yaml_raw

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

# A motor reads as 0 until its first value has arrived, which happens shortly after
# the first read of a freshly started process.
_SENSOR_WARMUP_S = 0.5

# Autotune entry script written temporarily to the project root.
# Imports Robot from src.hardware.robot and runs auto_tune() as a mission step:
# the setup mission first, then directly auto_tune(), without a time limit.
_AUTOTUNE_SCRIPT = """\
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raccoon import Sequential, Mission, auto_tune
from src.hardware.robot import Robot


class _AutotuneMission(Mission):
    def sequence(self) -> Sequential:
        return auto_tune()


async def _start_immediately(*_):
    pass


robot = Robot()
robot.missions = [_AutotuneMission()]
# No wait-for-light or button between setup and auto_tune(). A setup mission may
# bring its own gate, so both are replaced.
robot._pre_start_gate = _start_immediately
if robot.setup_mission is not None:
    robot.setup_mission.pre_start_gate = _start_immediately
# The match shutdown timer would cut the tuning off
robot.shutdown_in = 99999
robot.start()
"""

# Step response entry script written temporarily to the project root.
# Waits for the robot button, then records while a spinner screen is shown.
_STEP_RESPONSE_SCRIPT = """\
import sys, os, csv, time, argparse, asyncio, signal, threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raccoon.hal import Motor
from raccoon.ui import ProgressScreen, UIStep
from src.hardware.robot import Robot

parser = argparse.ArgumentParser()
parser.add_argument("--ports", nargs="+", type=int, default=[0])
parser.add_argument("--mode", choices=["speed", "velocity"], default="speed")
parser.add_argument("--speed", type=int, default=70)
parser.add_argument("--duration", type=float, default=3.0)
parser.add_argument("--brake-tail", type=float, default=2.0)
parser.add_argument("--hz", type=int, default=100)
parser.add_argument("--out", required=True)
args = parser.parse_args()

def _log(msg):
    sys.stderr.write(f"[step_response] {msg}\\n")
    sys.stderr.flush()

interval = 1.0 / args.hz
stop = threading.Event()

def record(motors):
    def brake_all():
        for m in motors:
            m.brake()

    def drive_all(value):
        for m in motors:
            if args.mode == "speed":
                m.set_speed(value)
            else:
                m.set_velocity(value)

    _log(f"ports={args.ports} mode={args.mode} speed={args.speed} "
         f"duration={args.duration}s hz={args.hz}")

    # A motor reads as 0 until its first value has arrived: read once and wait
    for m in motors:
        m.get_bemf()
        m.get_position()
    time.sleep(0.5)

    try:
        with open(args.out, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["sys_time", "elapsed_s", "brake_elapsed_s"]
                + [f"bemf_{p}" for p in args.ports]
                + [f"pos_{p}" for p in args.ports]
            )

            start = time.time()

            drive_all(args.speed)
            while not stop.is_set():
                now = time.time()
                elapsed = now - start
                if elapsed >= args.duration:
                    break
                bemfs = [m.get_bemf() for m in motors]
                positions = [m.get_position() for m in motors]
                writer.writerow([round(now, 6), round(elapsed, 5), ""] + bemfs + positions)
                time.sleep(interval)

            brake_all()
            brake_time = time.time()
            # The Pi server kills a cancelled command after 2 s, so keep the tail short
            tail = min(args.brake_tail, 1.0) if stop.is_set() else args.brake_tail
            _log(f"Braking - recording {tail}s tail...")

            while time.time() - brake_time < tail:
                now = time.time()
                elapsed = now - start
                brake_elapsed = now - brake_time
                bemfs = [m.get_bemf() for m in motors]
                positions = [m.get_position() for m in motors]
                writer.writerow(
                    [round(now, 6), round(elapsed, 5), round(brake_elapsed, 5)]
                    + bemfs + positions
                )
                time.sleep(interval)
    finally:
        for m in motors:
            m.off()

class _StepResponse(UIStep):
    aborted = False

    async def _execute_step(self, robot):
        button = asyncio.create_task(self.wait_for_button(
            "Press the button to start. The motors will spin.",
            title="Step Response",
        ))
        _log("Waiting for the button on the robot...")
        while not button.done():
            if stop.is_set():
                button.cancel()
                self.aborted = True
                return
            await asyncio.sleep(0.05)
        await button

        screen = ProgressScreen("Measuring step response...")
        screen.title = "Step Response"
        motors = [Motor(p) for p in args.ports]
        await self.run_with_ui(screen, asyncio.to_thread(record, motors))

async def main(robot):
    # Ctrl+C or a cancel request brakes the motors instead of killing the script
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    step = _StepResponse()
    await step.run_step(robot)
    return step.aborted

if asyncio.run(main(Robot())):
    _log("Aborted before the measurement started.")
    sys.exit(130)

print(f"DONE:{args.out}", flush=True)
_log("Done.")
"""


def _plot_step_response(csv_path: Path, out_path: Path, console: Console) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError:
        console.print("[yellow]matplotlib not installed - skipping plot.[/yellow]")
        return

    rows: list[dict] = []
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if not rows:
        console.print("[yellow]CSV is empty - skipping plot.[/yellow]")
        return

    def _float(v: str) -> float:
        return float(v) if v != "" else float("nan")

    cols: dict[str, list[float]] = {k: [] for k in rows[0]}
    for row in rows:
        for k, v in row.items():
            cols[k].append(_float(v))

    ports = sorted(int(k.split("_")[1]) for k in cols if k.startswith("bemf_"))
    elapsed = cols["elapsed_s"]

    brake_at: float | None = None
    for t, bv in zip(elapsed, cols["brake_elapsed_s"]):
        if not math.isnan(bv):
            brake_at = t
            break

    has_pos = f"pos_{ports[0]}" in cols
    n_rows = 2 if has_pos else 1
    fig, axes = plt.subplots(
        n_rows,
        len(ports),
        figsize=(max(6, 4 * len(ports)), 3.5 * n_rows),
        squeeze=False,
        sharex="col",
    )
    fig.suptitle(f"Motor Step Response - {csv_path.name}", fontsize=11)
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for ci, port in enumerate(ports):
        color = colors[ci % len(colors)]
        bemf = cols[f"bemf_{port}"]

        ax = axes[0][ci]
        ax.plot(elapsed, bemf, color=color, linewidth=0.9)
        ax.set_title(f"Motor {port} - BEMF")
        ax.set_ylabel("BEMF [raw]")
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.2f"))
        if brake_at is not None:
            ax.axvline(brake_at, color="crimson", linestyle="--", linewidth=1.2, label="brake")
            ax.legend(fontsize=7, loc="upper right")

        if has_pos:
            ax_p = axes[1][ci]
            ax_p.plot(elapsed, cols[f"pos_{port}"], color=color, linewidth=0.9)
            ax_p.set_ylabel("Position [ticks]")
            ax_p.set_xlabel("Time [s]")
            ax_p.grid(True, alpha=0.3)
            if brake_at is not None:
                ax_p.axvline(brake_at, color="crimson", linestyle="--", linewidth=1.2)
        else:
            ax.set_xlabel("Time [s]")

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    console.print(f"[green]Plot saved:[/green] {out_path}")

    if brake_at is not None:
        run_bemf = {
            port: [v for t, v in zip(elapsed, cols[f"bemf_{port}"]) if t < brake_at and not math.isnan(v)]
            for port in ports
        }
    else:
        run_bemf = {
            port: [v for v in cols[f"bemf_{port}"] if not math.isnan(v)]
            for port in ports
        }

    table = Table(title="BEMF - run phase")
    table.add_column("Motor", style="cyan")
    table.add_column("Mean", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Spread", justify="right")
    for port in ports:
        vals = run_bemf[port]
        if vals:
            mean = sum(vals) / len(vals)
            table.add_row(
                f"M{port}",
                f"{mean:+.1f}",
                f"{min(vals):+.0f}",
                f"{max(vals):+.0f}",
                f"{max(vals) - min(vals):.0f}",
            )
    console.print(table)


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

    for name, ticks in results.items():
        if ticks <= 0:
            console.print(f"[yellow]{name}: no ticks measured — not saved.[/yellow]")
            continue
        try:
            path = _save_motor_ticks_to_rad(project_root, name, (2 * math.pi) / ticks)
        except (OSError, KeyError) as exc:
            console.print(f"[red]Could not save ticks_to_rad for {name}: {exc}[/red]")
            continue
        console.print(f"[green]Saved ticks_to_rad of {name} to {path}[/green]")

# Calibrate Encoder Ticks Helpers

def _save_motor_ticks_to_rad(project_root: Path, motor_name: str, ticks_to_rad: float) -> Path:
    def _apply(motor: dict) -> None:
        if not motor.get("calibration"):
            motor["calibration"] = {}
        motor["calibration"]["ticks_to_rad"] = round(ticks_to_rad, 7)

    return _update_config_entry(project_root / "config" / "motors.yml", motor_name, _apply)


def _update_config_entry(path: Path, name: str, apply: Callable[[dict], None]) -> Path:
    """Change the top-level entry *name* of a hardware config file, keeping comments."""
    data = load_yaml_raw(path)
    if not isinstance(data.get(name), dict):
        raise KeyError(f"'{name}' not found in {path}")
    apply(data[name])
    save_yaml_raw(data, path)
    return path


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
    read_fn(port, inverted)  # discarded, see _SENSOR_WARMUP_S
    time.sleep(_SENSOR_WARMUP_S)

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

def _autotune_local(
    console: Console, project_root: Path, config: dict, codegen: bool = True
) -> None:
    console.print(Panel("[bold]Autotune Calibration[/bold]", border_style="cyan"))

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if codegen:
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
    await _calibrate_remote(
        ctx.obj["console"], project_root, config, ["autotune"], "autotune"
    )


async def _calibrate_remote(
    console: Console,
    project_root: Path,
    config: dict,
    args: list[str],
    label: str,
) -> None:
    """Run ``raccoon calibrate <args>`` on the Pi.

    Generates the hardware code, pushes the project, streams the output and
    pulls the results back. Raises SystemExit if the command fails.
    """
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
        console.print(f"[red]Sync failed — cannot run {label} remotely.[/red]")
        raise SystemExit(1)
    console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")

    console.print(f"[cyan]Running {label} on {state.pi_hostname}...[/cyan]")

    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as exc:
            console.print(f"[red]Failed to start {label} on Pi: {exc}[/red]")
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
            Panel.fit(f"Remote {label} {status_str} with code {exit_code}", style=style)
        )
        if exit_code != 0:
            raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# Calibrate Servos
# ---------------------------------------------------------------------------

def _calibrate_servos(ctx: click.Context, project_root: Path, config: dict) -> None:
    import httpx
    import logging
    import threading

    _httpx_logger = logging.getLogger("httpx")

    if sys.platform == "win32":
        import msvcrt

        def _read_key():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"): return "confirm"
            if ch == "\x03":       return "quit"
            if ch == "\x1b":       return "quit"
            if ch == "\xe0":
                ch2 = msvcrt.getwch()
                if ch2 == "H": return "up"
                if ch2 == "P": return "down"
                if ch2 == "K": return "left"
                if ch2 == "M": return "right"
            return "ignore"
    else:
        import tty
        import termios

        def _read_key():
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"): return "confirm"
            if ch == "\x03":       return "quit"
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A": return "up"
                if seq == "[B": return "down"
                if seq == "[D": return "left"
                if seq == "[C": return "right"
                return "quit"  # bare Esc
            return "ignore"

    console: Console = ctx.obj["console"]

    servo_defs = {
        name: defn
        for name, defn in config.get("definitions", {}).items()
        if isinstance(defn, dict)
           and defn.get("type") == "Servo"
           and defn.get("positions")
    }
    if not servo_defs:
        servos_yml = project_root / "config" / "servos.yml"
        if servos_yml.exists():
            import yaml
            extra = yaml.safe_load(servos_yml.read_text()) or {}
            servo_defs = {
                name: defn
                for name, defn in extra.items()
                if isinstance(defn, dict) and defn.get("positions")
            }
    if not servo_defs:
        console.print("[yellow]No servos with named positions found — aborting.[/yellow]")
        return

    address, port, api_token = _servo_server_endpoint(ctx.obj["local"])
    # Resolved once: looking up a .local name takes ~200 ms on every new connection
    server_url = f"http://{_resolve_ipv4(address, port)}:{port}"
    base_url = f"{server_url}/api/v1"
    request_headers = {"X-API-Token": api_token}

    console.print(f"[dim]Connecting to {address}:{port}...[/dim]")
    try:
        httpx.get(f"{server_url}/health", timeout=3.0).raise_for_status()
        console.print(f"[green]Connected to {address}:{port}[/green]")
    except Exception as exc:
        console.print(f"[red]Failed to connect to {address}:{port} — {exc}[/red]")
        return

    console.print(Panel("[bold]Servo Calibration[/bold]", border_style="cyan"))

    quit_all = False

    # One client for all requests, so key presses reuse the open connection
    client = httpx.Client()
    try:
        while not quit_all:
            servo_pick = _pick_servo(servo_defs)
            if servo_pick is None:
                break
            servo_name, defn = servo_pick

            pos_pick = _pick_position(servo_name, defn)
            if pos_pick is None:
                continue
            pos_name, nominal_deg = pos_pick

            port = int(defn.get("port", 0))
            current_offset = float(defn.get("offset", 0.0))
            start_deg = nominal_deg + current_offset

            try:
                r = client.post(
                    f"{base_url}/calibrate-servos/start",
                    json={"servo_id": servo_name, "servo_port": port, "initial_angle": start_deg},
                    headers=request_headers,
                    timeout=5.0,
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                console.print(f"[red]Could not start session ({exc.response.status_code}): {exc.response.text}[/red]")
                continue
            except Exception as exc:
                console.print(f"[red]Could not start session: {exc}[/red]")
                continue

            _step = 1.0
            _steps = [0.5, 1.0, 2.0, 5.0]
            _current_angle = [start_deg]  # mutable so worker can update it

            console.print(
                f"\n[bold cyan]Jogging {servo_name} · {pos_name}[/bold cyan]  "
                f"[dim](nominal {nominal_deg:.1f}°, offset {current_offset:+.1f}°, starting at {start_deg:.1f}°)[/dim]"
            )
            console.print("  [dim]↑ / ↓  move    ← / →  change step    Enter  confirm    Esc/Ctrl+C  quit[/dim]")
            sys.stdout.write(f"\n  [{_step:g}°]  Offset: +0.0°  (current: {start_deg:.1f}°)  ")
            sys.stdout.flush()

            skipped = False
            _move_queue: queue.Queue = queue.Queue()

            def _move_worker():
                while True:
                    delta = _next_move(_move_queue)
                    if delta is None:
                        break
                    try:
                        r = client.post(
                            f"{base_url}/calibrate-servos/move",
                            json={"delta_to_move": delta},
                            headers=request_headers,
                            timeout=2.0,
                        )
                        r.raise_for_status()
                        _current_angle[0] = r.json()["current_angle"]
                        offset = _current_angle[0] - start_deg
                        sys.stdout.write(f"\r  [{_step:g}°]  Offset: {offset:+.1f}°  (current: {_current_angle[0]:.1f}°)  ")
                        sys.stdout.flush()
                    except Exception as exc:
                        console.print(f"\n[red]Servo move failed: {exc}[/red]")

            worker = threading.Thread(target=_move_worker, daemon=True)
            worker.start()

            if sys.platform != "win32":
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                tty.setraw(fd)

            _httpx_logger.setLevel(logging.WARNING)
            try:
                while True:
                    key = _read_key()
                    if key == "confirm":
                        break
                    if key == "quit":
                        skipped = True
                        quit_all = True
                        break
                    if key == "ignore":
                        continue
                    if key == "left":
                        idx = _steps.index(_step)
                        _step = _steps[max(0, idx - 1)]
                        _ang = _current_angle[0]
                        sys.stdout.write(f"\r  [{_step:g}°]  Offset: {_ang - start_deg:+.1f}°  (current: {_ang:.1f}°)  ")
                        sys.stdout.flush()
                        continue
                    if key == "right":
                        idx = _steps.index(_step)
                        _step = _steps[min(len(_steps) - 1, idx + 1)]
                        _ang = _current_angle[0]
                        sys.stdout.write(f"\r  [{_step:g}°]  Offset: {_ang - start_deg:+.1f}°  (current: {_ang:.1f}°)  ")
                        sys.stdout.flush()
                        continue
                    if key == "up":
                        _move_queue.put(+_step)
                    if key == "down":
                        _move_queue.put(-_step)
            finally:
                _move_queue.put(None)
                worker.join()
                _httpx_logger.setLevel(logging.INFO)
                if sys.platform != "win32":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                sys.stdout.write("\n")
                sys.stdout.flush()

            end_data: dict | None = None
            try:
                end_resp = client.post(
                    f"{base_url}/calibrate-servos/end",
                    headers=request_headers,
                    timeout=5.0,
                )
                end_resp.raise_for_status()
                end_data = end_resp.json()
            except Exception as exc:
                console.print(f"[yellow]Failed to end calibration session: {exc}[/yellow]")

            try:
                if not skipped and end_data:
                    delta = end_data["delta"]
                    new_offset = current_offset + delta
                    saved_path = _save_servo_offset(project_root, servo_name, new_offset)
                    defn["offset"] = new_offset
                    console.print(
                        f"  delta: {delta:+.1f}°  final: {end_data['final_angle']:.1f}°  "
                        f"[green]saved offset {new_offset:+.1f}°[/green] [dim]({saved_path})[/dim]"
                    )
            except Exception as exc:
                console.print(f"[yellow]Could not persist servo offset: {exc}[/yellow]")

            if skipped and not quit_all:
                console.print(f"[dim]Skipped {servo_name} · {pos_name}[/dim]")

            if quit_all:
                break

            if not questionary.confirm("Calibrate another servo?", default=True, style=_STYLE).ask():
                break
    finally:
        client.close()

# Calibrate Servos Helpers
_DONE = object()

def _servo_server_endpoint(local: bool) -> tuple[str, int, str]:
    """Return (address, port, api_token) of the raccoon-server that moves the servos.

    In local mode this is the server running on this machine.
    """
    if local:
        from raccoon_cli.server.config import load_config

        server_config = load_config()
        return "127.0.0.1", server_config.port, server_config.api_token

    from raccoon_cli.client.connection import get_connection_manager

    state = get_connection_manager().state
    return state.pi_address, state.pi_port, state.api_token

def _resolve_ipv4(host: str, port: int) -> str:
    """Return the IPv4 address of *host*, or *host* itself if it cannot be resolved."""
    try:
        return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return host


def _next_move(moves: queue.Queue) -> Optional[float]:
    """Wait for the next jog and merge all jogs queued behind it into one.

    Returns None once the session is over (a None entry follows the last jog).
    """
    delta = moves.get()
    if delta is None:
        return None
    while True:
        try:
            queued = moves.get_nowait()
        except queue.Empty:
            return delta
        if queued is None:
            moves.put(None)
            return delta
        delta += queued


def _pick_servo(servo_defs: Dict[str, dict]) -> Optional[tuple[str, dict]]:
    choices = [
        questionary.Choice(
            title=f"{name}  ({', '.join(defn['positions'].keys())})",
            value=name,
        )
        for name, defn in servo_defs.items()
    ]
    choices.append(questionary.Choice(title="✕  Done", value=_DONE))
    result = questionary.select("Select servo to calibrate:", choices=choices, style=_STYLE).ask()
    if result is None or result is _DONE:
        return None
    return result, servo_defs[result]

def _pick_position(servo_name: str, defn: dict) -> Optional[tuple[str, float]]:
    positions: dict = defn["positions"]
    choices = [
        questionary.Choice(title=f"{pos_name}  ({deg}°)", value=pos_name)
        for pos_name, deg in positions.items()
    ]
    choices.append(questionary.Choice(title="✕  Cancel", value=_DONE))
    result = questionary.select(
        f"Select position to calibrate on {servo_name}:", choices=choices, style=_STYLE
    ).ask()
    if result is None or result is _DONE:
        return None
    return result, float(positions[result])


def _save_servo_offset(project_root: Path, servo_name: str, offset: float) -> Path:
    def _apply(servo: dict) -> None:
        servo["offset"] = round(offset, 3)

    return _update_config_entry(project_root / "config" / "servos.yml", servo_name, _apply)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def _prepare_calibration_context(ctx: click.Context, local: bool) -> None:
    """Load project state and resolve local vs remote execution once per invocation."""
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        config = load_project_config(project_root)
    except ProjectError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise SystemExit(1) from exc

    if not local:
        try:
            from raccoon_cli.client.connection import get_connection_manager

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
            # connect_sync reports an unreachable Pi through its return value
            if not manager.is_connected:
                console.print("[yellow]No Pi connection. Running locally.[/yellow]")
                local = True
        except Exception as exc:
            console.print(f"[yellow]No Pi connection ({exc}). Running locally.[/yellow]")
            local = True

    ctx.obj["local"] = local
    ctx.obj["project_root"] = project_root
    ctx.obj["config"] = config


def _prepare_calibration_subcommand_context(ctx: click.Context) -> None:
    parent = ctx.parent
    local = bool(parent.params.get("local", False)) if parent is not None else False
    _prepare_calibration_context(ctx, local)


def _wants_codegen(ctx: click.Context) -> bool:
    """False if the `calibrate` group was invoked with --no-codegen."""
    parent = ctx.parent
    return parent is None or not parent.params.get("no_codegen", False)


def _run_autotune(ctx: click.Context) -> None:
    console: Console = ctx.obj["console"]
    if ctx.obj["local"]:
        _autotune_local(
            console, ctx.obj["project_root"], ctx.obj["config"], codegen=_wants_codegen(ctx)
        )
    else:
        asyncio.run(_autotune_remote(ctx, ctx.obj["project_root"], ctx.obj["config"]))


def _run_step_response_local(
    console: Console,
    project_root: Path,
    config: dict,
    ports: list[str],
    mode: str,
    speed: int,
    duration: float,
    brake_tail: float,
    hz: int,
    csv_path: Path,
    codegen: bool = True,
) -> None:
    if codegen:
        pipeline = create_pipeline()
        pipeline.run_all(config, project_root / "src" / "hardware", format_code=True)

    script_path = project_root / "_raccoon_step_response.py"
    script_path.write_text(_STEP_RESPONSE_SCRIPT)

    cmd = [
        sys.executable,
        str(script_path),
        "--ports",
        *ports,
        "--mode",
        mode,
        "--speed",
        str(speed),
        "--duration",
        str(duration),
        "--brake-tail",
        str(brake_tail),
        "--hz",
        str(hz),
        "--out",
        str(csv_path),
    ]

    # A cancel from the Pi server arrives as SIGTERM: treat it like Ctrl+C so the
    # script gets to brake and the temporary file is removed.
    def _interrupt(sig, frame):
        raise KeyboardInterrupt

    original_sigterm = signal.signal(signal.SIGTERM, _interrupt)
    try:
        console.print(
            "[cyan]Press the button on the robot to start, Ctrl+C to brake early.[/cyan]"
        )
        console.print(f"[dim]CSV -> {csv_path}[/dim]\n")

        proc = subprocess.Popen(cmd, cwd=project_root)
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            console.print("\n[yellow]Ctrl+C - braking motors...[/yellow]")
            proc.terminate()
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()

        style = "bold green" if returncode == 0 else "bold red"
        console.print(Panel.fit(f"Recording finished (exit {returncode})", style=style))
        if returncode != 0:
            raise SystemExit(returncode)

        console.print(f"[green]CSV saved:[/green] {csv_path}")
    finally:
        signal.signal(signal.SIGTERM, original_sigterm)
        script_path.unlink(missing_ok=True)


def _run_step_response_remote(
    console: Console,
    project_root: Path,
    config: dict,
    ports: str,
    mode: str,
    speed: int,
    duration: float,
    brake_tail: float,
    hz: int,
    out: str,
) -> None:
    out_path = Path(out)
    if out_path.is_absolute() or ".." in out_path.parts:
        console.print("[red]--out must be a path inside the project for a remote run.[/red]")
        raise SystemExit(1)

    # Removed before the push: a stale CSV must neither be plotted nor win the
    # pull because the Pi's clock is behind.
    csv_path = project_root / out_path
    csv_path.unlink(missing_ok=True)

    args = [
        "step-response",
        "--ports", ports,
        "--mode", mode,
        "--speed", str(speed),
        "--duration", str(duration),
        "--brake-tail", str(brake_tail),
        "--hz", str(hz),
        "--out", out_path.as_posix(),
        "--no-plot",  # the plot is generated on this machine
    ]
    asyncio.run(_calibrate_remote(console, project_root, config, args, "step-response"))

    if not csv_path.exists():
        console.print(f"[red]No CSV received from the Pi: {csv_path}[/red]")
        raise SystemExit(1)


# Passed as help and short_help: click truncates a docstring in the command list
_AUTOTUNE_HELP = "Run a full autotune routine to tune the robot parameters to your exact bot"
_SERVOS_HELP = (
    "Calibrate servo offsets for when you've swapped servos and wanna compensate "
    "for the offset"
)
_STEP_RESPONSE_HELP = (
    "Record a motor step response and plot BEMF vs time (spins the motors; "
    "diagnostics only, doesn't change your config)"
)
_TICKS_HELP = "Measure and calibrate motor BEMF ticks for more accurate positioning"


@click.group(name="calibrate", no_args_is_help=True)
@click.option("--local", "-l", is_flag=True, help="Run locally on this machine (requires hardware)")
@click.option("--no-codegen", is_flag=True, help="Local runs: use the already generated hardware code")
@click.pass_context
def calibrate_group(
        ctx: click.Context, local: bool, no_codegen: bool
) -> None:
    """Robot calibration."""
    return


@calibrate_group.command(name="ticks", help=_TICKS_HELP, short_help=_TICKS_HELP)
@click.pass_context
def calibrate_ticks_cmd(ctx: click.Context) -> None:
    _prepare_calibration_subcommand_context(ctx)
    console = ctx.obj["console"]
    _calibrate_ticks(console, ctx.obj["project_root"], ctx.obj["config"], ctx.obj["local"])


@calibrate_group.command(name="autotune", help=_AUTOTUNE_HELP, short_help=_AUTOTUNE_HELP)
@click.pass_context
def calibrate_autotune_cmd(ctx: click.Context) -> None:
    _prepare_calibration_subcommand_context(ctx)
    _run_autotune(ctx)


@calibrate_group.command(name="servos", help=_SERVOS_HELP, short_help=_SERVOS_HELP)
@click.pass_context
def calibrate_servos_cmd(ctx: click.Context) -> None:
    _prepare_calibration_subcommand_context(ctx)
    _calibrate_servos(ctx, ctx.obj["project_root"], ctx.obj["config"])


@calibrate_group.command(name="step-response", help=_STEP_RESPONSE_HELP, short_help=_STEP_RESPONSE_HELP)
@click.option("--local", "force_local", is_flag=True, help="Run directly on this machine (requires hardware)")
@click.option("--ports", default="0,1,2,3", show_default=True, help="Comma-separated motor ports to record")
@click.option(
    "--mode",
    type=click.Choice(["speed", "velocity"]),
    default="speed",
    show_default=True,
    help="speed = set_speed() [%%], velocity = set_velocity() [BEMF units, PID]",
)
@click.option("--speed", default=70, show_default=True, help="Speed value (percent or BEMF units)")
@click.option("--duration", default=3.0, show_default=True, help="Run duration in seconds")
@click.option("--brake-tail", default=2.0, show_default=True, help="Extra seconds to record after braking")
@click.option("--hz", default=100, show_default=True, help="Sample rate in Hz")
@click.option("--out", default="step_response.csv", show_default=True, help="CSV output filename")
@click.option("--plot", default="step_response.png", show_default=True, help="Plot output filename")
@click.option("--no-plot", is_flag=True, help="Skip generating the plot")
@click.pass_context
def calibrate_step_response_cmd(
    ctx: click.Context,
    force_local: bool,
    ports: str,
    mode: str,
    speed: int,
    duration: float,
    brake_tail: float,
    hz: int,
    out: str,
    plot: str,
    no_plot: bool,
) -> None:
    parent = ctx.parent
    parent_local = bool(parent.params.get("local", False)) if parent is not None else False
    _prepare_calibration_context(ctx, parent_local or force_local)

    console: Console = ctx.obj["console"]
    project_root: Path = ctx.obj["project_root"]

    port_list = [p.strip() for p in ports.split(",") if p.strip()]
    console.print(
        Panel(
            f"[bold]Motor Step Response[/bold]\n"
            f"ports=[cyan]{ports}[/cyan]  mode=[cyan]{mode}[/cyan]  "
            f"speed=[cyan]{speed}[/cyan]  duration=[cyan]{duration}s[/cyan]  "
            f"brake_tail=[cyan]{brake_tail}s[/cyan]  hz=[cyan]{hz}[/cyan]",
            border_style="cyan",
        )
    )

    config: dict = ctx.obj["config"]
    csv_path = project_root / out
    plot_path = project_root / plot
    if ctx.obj["local"]:
        _run_step_response_local(
            console, project_root, config, port_list, mode, speed, duration,
            brake_tail, hz, csv_path, codegen=_wants_codegen(ctx),
        )
    else:
        _run_step_response_remote(
            console, project_root, config, ",".join(port_list), mode, speed,
            duration, brake_tail, hz, out,
        )
    if not no_plot:
        _plot_step_response(csv_path, plot_path, console)
