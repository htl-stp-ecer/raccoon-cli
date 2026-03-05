"""Motor PID/FF calibration."""

from __future__ import annotations

import asyncio
import csv
import logging
import re
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


def _resolve_validation_dir(project_root: Path, validation_output_dir: str | None) -> Path:
    if validation_output_dir:
        validation_path = Path(validation_output_dir)
        if not validation_path.is_absolute():
            validation_path = project_root / validation_path
        return validation_path
    return project_root / "logs" / "motor_validation"


def _select_plot_columns(headers: list[str]) -> tuple[str | None, list[str]]:
    header_map = {name.lower(): name for name in headers}

    def _find_name(matches: tuple[str, ...]) -> str | None:
        for key, original in header_map.items():
            if any(match in key for match in matches):
                return original
        return None

    time_col = _find_name(("time", "timestamp", "seconds", "ms"))
    if time_col:
        return time_col, [name for name in headers if name != time_col]

    command_col = _find_name(("command", "setpoint", "target", "input", "cmd", "power"))
    measured_col = _find_name(("measured", "velocity", "speed", "omega", "actual", "rate"))
    if command_col and measured_col and command_col != measured_col:
        return command_col, [measured_col]

    if len(headers) >= 2:
        return headers[0], headers[1:]

    return None, headers


def _parse_validation_filename(path: Path) -> tuple[str, float] | None:
    match = re.match(r"motor_(\d+)_cmd_([0-9.]+)\\.csv$", path.name)
    if not match:
        return None
    return match.group(1), float(match.group(2))


def _fit_command_scale(rows: list[dict[str, float]]) -> float:
    sum_x2 = 0.0
    sum_xy = 0.0
    for row in rows:
        command = row.get("command_percent")
        velocity = row.get("velocity_rad_s")
        if command is None or velocity is None:
            continue
        sum_x2 += command * command
        sum_xy += command * velocity
    if sum_x2 <= 0.0:
        return 0.0
    return sum_xy / sum_x2


def _build_command_sequence(commands: list[float]) -> list[float]:
    if not commands:
        return []

    unique = sorted(set(commands))
    zero_present = 0.0 in unique
    nonzero = [value for value in unique if value != 0.0]
    if not nonzero:
        return [0.0] if zero_present else []

    max_cmd = max(nonzero)
    if max_cmd == 0:
        return unique

    small_threshold = max_cmd * 0.3
    big_threshold = max_cmd * 0.7

    small_steps = [value for value in nonzero if value <= small_threshold]
    big_steps = [value for value in nonzero if value >= big_threshold]
    mid_steps = [value for value in nonzero if value not in small_steps and value not in big_steps]

    if not small_steps:
        small_steps = nonzero[:2]
    if not big_steps:
        big_steps = [max_cmd]

    sequence: list[float] = []
    sequence.extend(sorted(small_steps))

    if max_cmd not in sequence:
        sequence.append(max_cmd)

    sequence.extend(sorted(mid_steps, reverse=True))
    sequence.extend([value for value in sorted(small_steps, reverse=True) if value not in sequence])

    if zero_present:
        sequence.append(0.0)

    remaining = [value for value in nonzero if value not in sequence]
    sequence.extend(sorted(remaining))

    return sequence


def _aggregate_validation_profiles(
    console: Console,
    validation_dir: Path,
    csv_files: list[Path],
) -> tuple[set[Path], list[Path]]:
    grouped: dict[str, dict[float, list[Path]]] = {}
    for csv_path in csv_files:
        parsed = _parse_validation_filename(csv_path)
        if not parsed:
            continue
        motor_id, command = parsed
        grouped.setdefault(motor_id, {}).setdefault(command, []).append(csv_path)

    skip_files: set[Path] = set()
    combined_paths: list[Path] = []
    gap_seconds = 0.2

    for motor_id, command_map in grouped.items():
        commands = list(command_map.keys())
        sequence = _build_command_sequence(commands)
        combined_rows: list[dict[str, float]] = []
        time_offset = 0.0

        for command in sequence:
            csv_paths = command_map.get(command, [])
            if not csv_paths:
                continue

            for csv_path in csv_paths:
                try:
                    with open(csv_path, newline="", encoding="utf-8") as handle:
                        reader = csv.DictReader(handle)
                        rows = list(reader)
                except Exception as exc:
                    console.print(f"[yellow]Warning: Failed to read {csv_path.name}: {exc}[/yellow]")
                    continue

                if not rows or not reader.fieldnames:
                    continue

                time_values: list[float] = []
                for row in rows:
                    try:
                        time_value = float(row.get("time_s", ""))
                        velocity_value = float(row.get("velocity_rad_s", ""))
                        command_value = float(row.get("command_percent", command))
                    except (TypeError, ValueError):
                        continue

                    combined_rows.append(
                        {
                            "time_s": time_offset + time_value,
                            "command_percent": command_value,
                            "velocity_rad_s": velocity_value,
                        }
                    )
                    time_values.append(time_value)

                if time_values:
                    time_offset += max(time_values) + gap_seconds

                skip_files.add(csv_path)

        if 0.0 not in commands:
            placeholder_dt = 0.01
            placeholder_samples = 10
            for i in range(placeholder_samples):
                combined_rows.append(
                    {
                        "time_s": time_offset + (i * placeholder_dt),
                        "command_percent": 0.0,
                        "velocity_rad_s": 0.0,
                    }
                )
            time_offset += (placeholder_samples - 1) * placeholder_dt + gap_seconds

        if not combined_rows:
            continue

        scale = _fit_command_scale(combined_rows)
        for row in combined_rows:
            row["command_rad_s"] = row["command_percent"] * scale

        combined_path = validation_dir / f"motor_{motor_id}_queued.csv"
        try:
            with open(combined_path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["time_s", "command_percent", "command_rad_s", "velocity_rad_s"],
                )
                writer.writeheader()
                writer.writerows(combined_rows)
            combined_paths.append(combined_path)
        except Exception as exc:
            console.print(f"[yellow]Warning: Failed to write {combined_path.name}: {exc}[/yellow]")

    return skip_files, combined_paths


def _plot_queued_profile(console: Console, csv_path: Path) -> None:
    try:
        with open(csv_path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except Exception as exc:
        console.print(f"[yellow]Warning: Failed to read {csv_path.name}: {exc}[/yellow]")
        return

    if not rows or not reader.fieldnames:
        return

    time_vals: list[float] = []
    velocity_vals: list[float] = []
    command_vals: list[float] = []

    for row in rows:
        try:
            time_vals.append(float(row.get("time_s", "")))
            velocity_vals.append(float(row.get("velocity_rad_s", "")))
            command_vals.append(float(row.get("command_rad_s", "")))
        except (TypeError, ValueError):
            continue

    if not time_vals or not velocity_vals:
        return

    import matplotlib.pyplot as plt

    plt.figure(figsize=(8, 5))
    plt.plot(time_vals, velocity_vals, marker="o", markersize=2, linewidth=1, label="velocity_rad_s")
    if command_vals:
        plt.plot(time_vals, command_vals, marker="o", markersize=2, linewidth=1, label="command_rad_s")

    plt.title(csv_path.stem)
    plt.xlabel("time_s")
    plt.ylabel("rad/s")
    plt.legend()
    plt.tight_layout()

    output_path = csv_path.with_suffix(".png")
    try:
        plt.savefig(output_path, dpi=160)
    except Exception as exc:
        console.print(f"[yellow]Warning: Failed to write plot {output_path.name}: {exc}[/yellow]")
    finally:
        plt.close()


def _generate_validation_plots(console: Console, validation_dir: Path) -> None:
    if not validation_dir.exists():
        return

    csv_files = sorted(validation_dir.glob("*.csv"))
    if not csv_files:
        return

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        console.print(
            f"[yellow]Warning: Could not generate validation plots (matplotlib unavailable: {exc}).[/yellow]"
        )
        return

    skip_files, combined_paths = _aggregate_validation_profiles(console, validation_dir, csv_files)

    for combined_path in combined_paths:
        _plot_queued_profile(console, combined_path)

    for csv_path in csv_files:
        if csv_path in skip_files:
            continue
        try:
            with open(csv_path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
        except Exception as exc:
            console.print(f"[yellow]Warning: Failed to read {csv_path.name}: {exc}[/yellow]")
            continue

        if not rows or not reader.fieldnames:
            continue

        numeric_columns: dict[str, list[float | None]] = {name: [] for name in reader.fieldnames}
        for row in rows:
            for name in reader.fieldnames:
                value = row.get(name, "")
                try:
                    numeric_columns[name].append(float(value))
                except (TypeError, ValueError):
                    numeric_columns[name].append(None)

        headers = [name for name in reader.fieldnames if any(v is not None for v in numeric_columns[name])]
        if not headers:
            continue

        x_name, y_names = _select_plot_columns(headers)
        x_vals: list[float] | None = None
        if x_name:
            x_vals = [v for v in numeric_columns[x_name] if v is not None]
            if not x_vals:
                x_name = None

        plt.figure(figsize=(8, 5))
        plotted = False

        for y_name in y_names:
            y_raw = numeric_columns.get(y_name, [])
            if not any(v is not None for v in y_raw):
                continue

            if x_name and x_vals is not None:
                points = [
                    (x_val, y_val)
                    for x_val, y_val in zip(numeric_columns[x_name], y_raw)
                    if x_val is not None and y_val is not None
                ]
                if not points:
                    continue
                xs, ys = zip(*points)
                if x_name != y_name:
                    plt.plot(xs, ys, marker="o", markersize=2, linewidth=1, label=y_name)
                else:
                    plt.plot(xs, ys, marker="o", markersize=2, linewidth=1)
            else:
                ys = [v for v in y_raw if v is not None]
                if not ys:
                    continue
                plt.plot(ys, marker="o", markersize=2, linewidth=1, label=y_name)

            plotted = True

        if not plotted:
            plt.close()
            continue

        plt.title(csv_path.stem)
        if x_name and x_vals is not None:
            plt.xlabel(x_name)
        else:
            plt.xlabel("sample")
        plt.ylabel("value")
        if len(y_names) > 1:
            plt.legend()
        plt.tight_layout()

        output_path = csv_path.with_suffix(".png")
        try:
            plt.savefig(output_path, dpi=160)
        except Exception as exc:
            console.print(f"[yellow]Warning: Failed to write plot {output_path.name}: {exc}[/yellow]")
        finally:
            plt.close()


def _average_calibration_results(all_results: list, console: Console) -> list:
    """Average multiple calibration runs to reduce BEMF noise.

    Args:
        all_results: List of result lists from multiple calibration runs
        console: Rich console for output

    Returns:
        List of averaged results (same structure as single run)
    """
    if not all_results or not all_results[0]:
        return []

    num_motors = len(all_results[0])
    num_runs = len(all_results)

    console.print(f"\n[cyan]Averaging {num_runs} calibration runs...[/cyan]")

    # Create averaged results
    averaged = []
    for motor_idx in range(num_motors):
        # Collect all successful results for this motor
        successful_runs = [
            run[motor_idx] for run in all_results
            if run[motor_idx].success
        ]

        if not successful_runs:
            # No successful runs - use first result (will be marked as failed)
            averaged.append(all_results[0][motor_idx])
            continue

        # Average the values
        avg_kp = sum(r.pid.kp for r in successful_runs) / len(successful_runs)
        avg_ki = sum(r.pid.ki for r in successful_runs) / len(successful_runs)
        avg_kd = sum(r.pid.kd for r in successful_runs) / len(successful_runs)
        avg_kS = sum(r.ff.kS for r in successful_runs) / len(successful_runs)
        avg_kV = sum(r.ff.kV for r in successful_runs) / len(successful_runs)
        avg_kA = sum(r.ff.kA for r in successful_runs) / len(successful_runs)

        # Create a result-like object with averaged values
        # We'll use the first successful result as template and update values
        template = successful_runs[0]

        # Create new PID and FF objects with averaged values
        class AvgResult:
            def __init__(self):
                self.success = True
                self.pid = type('PID', (), {'kp': avg_kp, 'ki': avg_ki, 'kd': avg_kd})()
                self.ff = type('FF', (), {'kS': avg_kS, 'kV': avg_kV, 'kA': avg_kA})()

        averaged.append(AvgResult())

        console.print(f"  Motor {motor_idx}: {len(successful_runs)}/{num_runs} successful runs averaged")

    return averaged


def calibrate_motors_local(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    aggressive: bool,
    auto_save: bool = False,
    export_validation: bool = True,
    validation_output_dir: str | None = None,
    iterations: int = 1,
) -> None:
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

    iterations_msg = f"\nIterations: [yellow]{iterations}[/yellow] (results will be averaged)" if iterations > 1 else ""
    console.print(Panel(
        f"[bold cyan]Starting Motor Calibration[/bold cyan]\n"
        f"Mode: {'[yellow]Aggressive (relay feedback)[/yellow]' if aggressive else '[green]Standard[/green]'}\n"
        f"Project: {config.get('name', 'Unknown')}{iterations_msg}",
        border_style="cyan"
    ))

    # Create robot instance
    try:
        robot = Robot()
    except Exception as exc:
        console.print(f"[red]Failed to initialize robot: {exc}[/red]")
        raise SystemExit(1) from exc

    # Run calibration (potentially multiple times)
    all_results = []

    for iteration in range(iterations):
        if iterations > 1:
            console.print(f"\n[cyan]Running calibration iteration {iteration + 1}/{iterations}...[/cyan]")
        else:
            console.print("\n[cyan]Running calibration... This may take a few moments.[/cyan]")

        try:
            calibration_config = None
            if aggressive or export_validation or validation_output_dir:
                calibration_config = CalibrationConfig()
                if aggressive:
                    calibration_config.use_relay_feedback = True
                if export_validation:
                    calibration_config.export_validation_profiles = True
                if validation_output_dir:
                    # Append iteration number to validation dir for multiple runs
                    if iterations > 1:
                        iter_dir = f"{validation_output_dir}_iter{iteration + 1}"
                    else:
                        iter_dir = validation_output_dir
                    calibration_config.validation_output_dir = str(iter_dir)
            if calibration_config is not None:
                results = robot.kinematics.calibrate_motors(calibration_config)
            else:
                results = robot.kinematics.calibrate_motors()

            all_results.append(results)

        except Exception as exc:
            console.print(f"\n[red]Calibration iteration {iteration + 1} failed: {exc}[/red]")
            if iteration == 0:
                # First iteration failed - can't continue
                raise SystemExit(1) from exc
            # Later iterations can fail - we'll average what we have
            console.print("[yellow]Continuing with remaining iterations...[/yellow]")

    # Average results if multiple iterations
    if iterations > 1 and len(all_results) > 1:
        results = _average_calibration_results(all_results, console)
    elif all_results:
        results = all_results[0]
    else:
        console.print("[red]No successful calibration runs.[/red]")
        raise SystemExit(1)

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
        from raccoon.yaml_utils import save_yaml

        save_yaml(config, config_path)

        console.print(f"\n[green]✓ Calibration results saved to {config_path.relative_to(project_root)}[/green]")
    except Exception as exc:
        console.print(f"\n[red]Failed to save configuration: {exc}[/red]")
        raise SystemExit(1) from exc

    if export_validation:
        validation_dir = _resolve_validation_dir(project_root, validation_output_dir)
        _generate_validation_plots(console, validation_dir)


async def calibrate_motors_remote(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    aggressive: bool,
    export_validation: bool = True,
    validation_output_dir: str | None = None,
    iterations: int = 1,
) -> None:
    """Run motor PID/FF calibration on the connected Pi."""
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.client.sftp_sync import SyncDirection
    from raccoon.commands.sync_cmd import sync_project_interactive

    # Sync project to Pi before calibration
    if not sync_project_interactive(project_root, console):
        console.print("[red]Sync failed, cannot run calibration remotely[/red]")
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
    if not export_validation:
        args.append("--no-export-validation")
    if validation_output_dir:
        args.extend(["--validation-output-dir", str(validation_output_dir)])
    if iterations > 1:
        args.extend(["--iterations", str(iterations)])

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

    console.print()
    console.print("[dim]Syncing calibration results...[/dim]")
    sync_ok = sync_project_interactive(project_root, console, direction=SyncDirection.PULL)
    if sync_ok:
        console.print("[green]✓ Calibration results synced to local project[/green]")
    else:
        console.print("[yellow]Warning: Failed to sync results. Run 'raccoon sync' manually.[/yellow]")

    if export_validation and sync_ok:
        validation_dir = _resolve_validation_dir(project_root, validation_output_dir)
        _generate_validation_plots(console, validation_dir)

    if exit_code == 0:
        console.print()
        console.print("[green]Calibration completed on Pi![/green]")
        return

    console.print()
    console.print(f"[red]Calibration failed with exit code {exit_code}[/red]")
    raise SystemExit(exit_code)
