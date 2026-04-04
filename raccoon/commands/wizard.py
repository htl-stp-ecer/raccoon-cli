"""Interactive project setup wizard."""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Dict, Optional, Tuple

import click
import questionary
import yaml
from questionary import Style as QStyle
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from raccoon.project import ProjectError, load_project_config, require_project, save_project_keys

logger = logging.getLogger("raccoon")

# ---------------------------------------------------------------------------
# Questionary style — consistent with raccoon's purple/cyan brand
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _int_validator(v: str) -> bool | str:
    return True if v.lstrip("-").isdigit() else "Please enter a whole number."


def _pos_float_validator(v: str) -> bool | str:
    try:
        if float(v) > 0:
            return True
        return "Value must be greater than 0."
    except ValueError:
        return "Please enter a number."


def _alpha_validator(v: str) -> bool | str:
    try:
        f = float(v)
        if 0.0 < f <= 1.0:
            return True
        return "Must be between 0 (exclusive) and 1 (inclusive)."
    except ValueError:
        return "Please enter a number between 0 and 1."


# ---------------------------------------------------------------------------
# Step 0 — Pi Connection
# ---------------------------------------------------------------------------

def _connect_step(console: Console) -> bool:
    """Ask if the user wants to connect to a Pi. Returns True if connected."""
    from raccoon.client.connection import get_connection_manager, ParamikoVersionError

    manager = get_connection_manager()

    # Already connected?
    if manager.is_connected:
        console.print(f"[green]Already connected to {manager.state.pi_hostname}[/green]")
        return True

    want = questionary.confirm(
        "Connect to a Pi? (enables live encoder calibration and syncing later)",
        default=True,
        style=_STYLE,
    ).ask()
    if not want:
        return False

    # Build choice list: known Pis + manual entry option
    known = manager.load_known_pis()
    MANUAL = "✏  Enter address manually"
    choices = [f"{p.get('address')}:{p.get('port', 8421)}" for p in known] + [MANUAL]

    if len(choices) > 1:
        choice = questionary.select("Choose Pi:", choices=choices, style=_STYLE).ask()
    else:
        choice = MANUAL

    if choice is None:
        return False

    if choice == MANUAL:
        address = questionary.text(
            "Pi address:",
            default="192.168.4.1",
            validate=lambda v: True if v.strip() else "Enter an IP address or hostname.",
            style=_STYLE,
        ).ask()
        if not address:
            return False
        port_str = questionary.text("Port:", default="8421",
                                    validate=_int_validator, style=_STYLE).ask()
        port = int(port_str or 8421)
        user = questionary.text("SSH user:", default="pi", style=_STYLE).ask() or "pi"
    else:
        parts = choice.rsplit(":", 1)
        address, port = parts[0], int(parts[1])
        user = "pi"

    console.print(f"[cyan]Connecting to {address}:{port}...[/cyan]")
    try:
        manager.connect_sync(address, port, user)
        console.print(f"[green]Connected to {manager.state.pi_hostname}[/green]")
        manager.save_to_project(Path.cwd())
        manager.save_to_global()
        return True
    except ParamikoVersionError as exc:
        console.print(f"[red]Paramiko version error: {exc}[/red]")
        return False
    except Exception as exc:
        console.print(f"[yellow]Could not connect: {exc}[/yellow]")
        return False


# ---------------------------------------------------------------------------
# Step 1 — Project name
# ---------------------------------------------------------------------------

def _ask_project_name(existing: str) -> str:
    name = questionary.text(
        "Project name:", default=existing or "My Raccoon Robot", style=_STYLE
    ).ask()
    return name or existing or "My Raccoon Robot"


# ---------------------------------------------------------------------------
# Step 2 — Drivetrain
# ---------------------------------------------------------------------------

_DRIVETRAIN_CHOICES = [
    questionary.Choice("Mecanum (4-wheel holonomic)", value="mecanum"),
    questionary.Choice("Differential (2-wheel tank)",  value="differential"),
]


def _ask_drivetrain(existing: Optional[str]) -> str:
    default = "mecanum" if existing not in ("mecanum", "differential") else existing
    result = questionary.select(
        "Drivetrain type:", choices=_DRIVETRAIN_CHOICES, default=default, style=_STYLE
    ).ask()
    return result or default


# ---------------------------------------------------------------------------
# Step 3 — Motor slots
# ---------------------------------------------------------------------------

_MOTOR_SLOTS = {
    "mecanum": [
        ("front_left_motor",  "Front-left"),
        ("front_right_motor", "Front-right"),
        ("rear_left_motor",   "Rear-left"),
        ("rear_right_motor",  "Rear-right"),
    ],
    "differential": [
        ("left_motor",  "Left"),
        ("right_motor", "Right"),
    ],
}


def _ask_motors(drivetrain: str, existing_defs: Dict) -> Dict[str, Tuple[int, bool]]:
    """Return {slot_name: (port, inverted)} for each drive motor."""
    slots = _MOTOR_SLOTS[drivetrain]
    motors: Dict[str, Tuple[int, bool]] = {}
    default_port = 0

    for key, label in slots:
        console_label = f"[bold]{label} motor[/bold]"
        print(f"\n  {label} motor")

        existing = existing_defs.get(key, {})
        ex_port    = existing.get("port", default_port) if isinstance(existing, dict) else default_port
        ex_inv     = existing.get("inverted", key.endswith("right_motor")) if isinstance(existing, dict) else key.endswith("right_motor")

        port_raw = questionary.select(
            f"  port:", choices=_MOTOR_PORT_CHOICES,
            default=str(ex_port), style=_STYLE
        ).ask()
        if port_raw is None:
            port_raw = str(ex_port)

        inverted = questionary.confirm(
            f"  inverted?", default=ex_inv, style=_STYLE
        ).ask()
        if inverted is None:
            inverted = ex_inv

        motors[key] = (int(port_raw), inverted)
        default_port += 1

    return motors


# ---------------------------------------------------------------------------
# Step 4 — Button sensor (required by defs generator)
# ---------------------------------------------------------------------------

_BUTTON_PORT_CHOICES = [str(i) for i in range(11)]  # 0-10; Wombat has 10 digital ports


def _ask_button(existing_defs: Dict) -> int:
    """Return port for the start button DigitalSensor."""
    print("\n  Button sensor (required — start/stop trigger)")
    ex = existing_defs.get("button", {})
    ex_port = ex.get("port", 10) if isinstance(ex, dict) else 10

    port_raw = questionary.select(
        "  port:", choices=_BUTTON_PORT_CHOICES,
        default=str(ex_port), style=_STYLE
    ).ask()
    return int(port_raw or ex_port)


# ---------------------------------------------------------------------------
# Step 5 — Physical measurements
# ---------------------------------------------------------------------------

def _ask_measurements(existing_robot: Dict) -> Dict[str, float]:
    """Collect physical dimensions with labelled, validated prompts."""
    kin = existing_robot.get("drive", {}).get("kinematics", {})

    ex_wr  = kin.get("wheel_radius")
    ex_tw  = kin.get("track_width")
    ex_wb  = kin.get("wheelbase")

    def_diam  = round(ex_wr * 2 * 1000, 1) if ex_wr else 75.0
    def_track = round(ex_tw * 100, 1)       if ex_tw else 20.0
    def_wb    = round(ex_wb * 100, 1)       if ex_wb else 15.0

    # Try vel_lpf_alpha from any motor
    def_alpha = 0.8

    print("\n  Physical measurements")

    diam  = questionary.text("  Wheel diameter (mm):", default=str(def_diam),
                              validate=_pos_float_validator, style=_STYLE).ask()
    track = questionary.text("  Track width cm (L↔R wheel centres):", default=str(def_track),
                              validate=_pos_float_validator, style=_STYLE).ask()
    wb    = questionary.text("  Wheelbase cm (front↔rear axle):", default=str(def_wb),
                              validate=_pos_float_validator, style=_STYLE).ask()
    alpha = questionary.text("  Velocity low-pass alpha (0–1):", default=str(def_alpha),
                              validate=_alpha_validator, style=_STYLE).ask()

    return {
        "wheel_diameter_mm": float(diam   or def_diam),
        "track_width_cm":    float(track  or def_track),
        "wheelbase_cm":      float(wb     or def_wb),
        "vel_filter_alpha":  float(alpha  or def_alpha),
    }


# ---------------------------------------------------------------------------
# Step 6 — Encoder ticks calibration (optional)
# ---------------------------------------------------------------------------

def _read_remote(address: str, port: int, api_token: Optional[str], motor_port: int, inverted: bool) -> int:
    from raccoon.client.api import create_api_client

    async def _inner():
        async with create_api_client(address, port, api_token=api_token) as client:
            r = await client.read_encoder(motor_port, inverted)
            if not r.success:
                raise RuntimeError(r.error)
            return r.position

    return asyncio.run(_inner())


def _measure_ticks_for_motor(
    name: str, motor_port: int, inverted: bool,
    read_fn,
    num_trials: int = 3,
) -> float:
    """Guide the user through rotating one wheel and return avg ticks/rev."""
    measurements = []
    for trial in range(1, num_trials + 1):
        print(f"\n    Trial {trial}/{num_trials} — {name}")
        start = read_fn(motor_port, inverted)
        print(f"    Encoder: {start}")
        questionary.text(
            "    Rotate wheel exactly ONE full turn (360°) then press Enter:",
            default="", style=_STYLE
        ).ask()
        end = read_fn(motor_port, inverted)
        ticks = abs(end - start)
        print(f"    → {ticks} ticks")
        measurements.append(ticks)

    avg = sum(measurements) / len(measurements)
    print(f"    {name}: {avg:.1f} ticks/rev (average)")
    return avg


def _ask_ticks(
    motor_defs: Dict[str, Tuple[int, bool]],
    existing_defs: Dict,
    is_connected: bool,
) -> Dict[str, int]:
    """Optionally calibrate encoder ticks per revolution. Returns {motor: ticks}."""
    # Build defaults from existing config
    defaults: Dict[str, int] = {}
    for name in motor_defs:
        defn = existing_defs.get(name, {})
        if isinstance(defn, dict):
            calib = defn.get("calibration", {})
            t2r = calib.get("ticks_to_rad") if isinstance(calib, dict) else None
            if t2r and t2r > 0:
                defaults[name] = int(round((2 * math.pi) / t2r))
    for name in motor_defs:
        defaults.setdefault(name, 1536)

    single_default = next(iter(defaults.values()), 1536)

    print()
    run_cal = questionary.select(
        "Encoder ticks calibration (optional):",
        choices=[
            questionary.Choice("Run guided calibration (rotate each wheel by hand)", value="guided"),
            questionary.Choice(f"Enter ticks manually (current default: {single_default})",  value="manual"),
            questionary.Choice("Skip — keep existing values",                                  value="skip"),
        ],
        style=_STYLE,
    ).ask()

    if run_cal is None or run_cal == "skip":
        return defaults

    if run_cal == "manual":
        raw = questionary.text(
            "Ticks per wheel revolution:",
            default=str(single_default),
            validate=lambda v: _int_validator(v) and int(v) > 0 or "Must be a positive integer.",
            style=_STYLE,
        ).ask()
        ticks = int(raw or single_default)
        return {name: ticks for name in motor_defs}

    # Guided calibration
    read_fn = None

    if is_connected:
        try:
            from raccoon.client.connection import get_connection_manager
            manager = get_connection_manager()
            if manager.is_connected:
                state = manager.state
                read_fn = lambda mp, inv: _read_remote(state.pi_address, state.pi_port, state.api_token, mp, inv)
        except Exception:
            pass

    if read_fn is None:
        # Try local HAL
        try:
            from libstp.hal import Motor as HalMotor  # type: ignore

            def _local_read(mp: int, inv: bool) -> int:
                return HalMotor(port=mp, inverted=inv).get_position()

            read_fn = _local_read
        except Exception as exc:
            print(f"  [yellow]No hardware available for live ticks ({exc}). Falling back to manual.[/yellow]")
            raw = questionary.text(
                "Ticks per wheel revolution:",
                default=str(single_default), validate=_int_validator, style=_STYLE
            ).ask()
            ticks = int(raw or single_default)
            return {name: ticks for name in motor_defs}

    results: Dict[str, int] = {}
    for name, (mp, inv) in motor_defs.items():
        print(f"\n  {name} (port {mp})")
        print("  Make sure the wheel can spin freely.")
        go = questionary.confirm(f"  Calibrate {name}?", default=True, style=_STYLE).ask()
        if not go:
            results[name] = defaults.get(name, 1536)
            continue
        try:
            avg = _measure_ticks_for_motor(name, mp, inv, read_fn)
            results[name] = int(round(avg))
        except Exception as exc:
            print(f"  Error: {exc} — using default {defaults.get(name, 1536)}")
            results[name] = defaults.get(name, 1536)

    return results


# ---------------------------------------------------------------------------
# Config builders (unchanged logic, kept from original wizard)
# ---------------------------------------------------------------------------

def _build_motor_def(port: int, inverted: bool, ticks_to_rad: float, vel_lpf_alpha: float) -> Dict:
    return {
        "type": "Motor",
        "port": port,
        "inverted": inverted,
        "calibration": {
            "ff":  {"kS": 0.08, "kV": 0.12, "kA": 0.1},
            "pid": {"kp": 2.4,  "ki": 0.3,  "kd": 0.08},
            "ticks_to_rad":  round(ticks_to_rad, 7),
            "vel_lpf_alpha": round(vel_lpf_alpha, 3),
        },
    }


def _build_definitions(
    motors: Dict[str, Tuple[int, bool]],
    button_port: int,
    ticks_to_rad: Dict[str, float],
    vel_lpf_alpha: float,
) -> Dict:
    defs: Dict = {"imu": {"type": "IMU"}}
    for name, (port, inverted) in motors.items():
        defs[name] = _build_motor_def(port, inverted, ticks_to_rad[name], vel_lpf_alpha)
    defs["button"] = {"type": "DigitalSensor", "port": button_port}
    return defs


def _build_kinematics(drivetrain: str, motors: Dict[str, Tuple[int, bool]], m: Dict[str, float]) -> Dict:
    wheel_radius = (m["wheel_diameter_mm"] / 1000.0) / 2.0
    track_width  = m["track_width_cm"] / 100.0
    wheelbase    = m["wheelbase_cm"] / 100.0

    cfg: Dict = {
        "type":         drivetrain,
        "wheel_radius": round(wheel_radius, 5),
        "track_width":  round(track_width, 4),
    }
    if drivetrain == "mecanum":
        cfg["wheelbase"]         = round(wheelbase, 4)
        cfg["front_left_motor"]  = "front_left_motor"
        cfg["front_right_motor"] = "front_right_motor"
        cfg["back_left_motor"]   = "rear_left_motor"
        cfg["back_right_motor"]  = "rear_right_motor"
    else:
        cfg["left_motor"]  = "left_motor"
        cfg["right_motor"] = "right_motor"
    return cfg


def _build_robot(drivetrain: str, motors: Dict[str, Tuple[int, bool]], m: Dict[str, float]) -> Dict:
    return {
        "drive":    {"kinematics": _build_kinematics(drivetrain, motors, m)},
        "odometry": {"type": "FusedOdometry", "invert_x": False, "invert_y": False,
                     "invert_z": True, "invert_w": False},
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _to_builtin(obj):
    if isinstance(obj, dict):
        return {k: _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_builtin(v) for v in obj]
    return obj


def _render_summary(console: Console, config: Dict) -> None:
    table = Table(title="Wizard Summary", expand=True)
    table.add_column("Section", style="bold cyan", no_wrap=True)
    table.add_column("Details")
    table.add_row("Project",     f"name: {config.get('name', '—')}\nuuid: {config.get('uuid', '—')}")
    table.add_row("Drive",       yaml.safe_dump(config.get("robot", {}), sort_keys=False).strip())
    table.add_row("Definitions", yaml.safe_dump(_to_builtin(config.get("definitions", {})), sort_keys=False).strip())
    console.print(Panel(table, border_style="green"))


# ---------------------------------------------------------------------------
# Wizard command
# ---------------------------------------------------------------------------

@click.command(name="wizard")
@click.option("--dry-run", is_flag=True, help="Preview output without writing raccoon.project.yml")
@click.pass_context
def wizard_command(ctx: click.Context, dry_run: bool) -> None:
    """Interactive wizard to scaffold or update raccoon.project.yml.

    Guides you through drivetrain type, motor ports, physical measurements,
    button sensor, optional extra definitions, and encoder ticks calibration.
    Hardware types and their parameters are read directly from the installed
    libstp stubs so the wizard always reflects the available API.
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    try:
        existing = load_project_config(project_root)
    except ProjectError:
        existing = {}

    ex_robot = existing.get("robot", {})
    if not isinstance(ex_robot, dict):
        ex_robot = {}
    ex_defs = existing.get("definitions", {})
    if not isinstance(ex_defs, dict):
        ex_defs = {}

    console.print(Panel("[bold cyan]Raccoon Project Wizard[/bold cyan]\n"
                        "[dim]Arrow keys to navigate · Enter to confirm · Ctrl-C to abort[/dim]",
                        border_style="cyan"))
    print()

    # ── 0. Connection ────────────────────────────────────────────────────────
    is_connected = _connect_step(console)
    print()

    # ── 1. Project name ──────────────────────────────────────────────────────
    project_name = _ask_project_name(existing.get("name", ""))
    print()

    # ── 2. Drivetrain ────────────────────────────────────────────────────────
    existing_drive_type = (ex_robot.get("drive", {}) or {}).get("kinematics", {}).get("type")
    drivetrain = _ask_drivetrain(existing_drive_type)
    print()

    # ── 3. Motors ────────────────────────────────────────────────────────────
    print("  [Drive motors]")
    motor_defs = _ask_motors(drivetrain, ex_defs)
    print()

    # ── 4. Button ────────────────────────────────────────────────────────────
    button_port = _ask_button(ex_defs)
    print()

    # ── 5. Physical measurements ─────────────────────────────────────────────
    measurements = _ask_measurements(ex_robot)
    print()

    # ── 6. Encoder ticks (optional) ──────────────────────────────────────────
    ticks_per_rev = _ask_ticks(motor_defs, ex_defs, is_connected)

    # ── Build config ──────────────────────────────────────────────────────────
    ticks_to_rad = {name: (2 * math.pi) / ticks for name, ticks in ticks_per_rev.items()}

    config: Dict = dict(existing)
    config["name"] = project_name
    config.setdefault("uuid", existing.get("uuid", ""))
    config["robot"] = _build_robot(drivetrain, motor_defs, measurements)

    merged_defs: Dict = dict(ex_defs)
    merged_defs.update(_build_definitions(
        motor_defs, button_port,
        ticks_to_rad, measurements["vel_filter_alpha"],
    ))
    config["definitions"] = merged_defs

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    _render_summary(console, config)

    if dry_run:
        console.print("[yellow]Dry run — raccoon.project.yml was not updated.[/yellow]")
        return

    confirm = questionary.confirm("Save configuration?", default=True, style=_STYLE).ask()
    if not confirm:
        console.print("[yellow]Aborted — nothing was saved.[/yellow]")
        return

    save_project_keys(project_root, {
        "name":        config["name"],
        "uuid":        config.get("uuid", ""),
        "robot":       config["robot"],
        "definitions": config["definitions"],
    })

    console.print("[green]raccoon.project.yml updated.[/green]")
    console.print(
        Panel(
            "[bold yellow]Next step — physical configuration[/bold yellow]\n\n"
            "Open the web IDE ([cyan]raccoon web[/cyan]) and go to the [bold]Device[/bold] tab "
            "to set your robot's physical dimensions, sensor positions, rotation centre, "
            "and starting pose.\n\n"
            "[dim]The robot will not navigate correctly until these values are configured.[/dim]",
            border_style="yellow",
            expand=False,
        )
    )
