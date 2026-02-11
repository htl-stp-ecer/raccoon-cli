"""Motor PID benchmark - test responsiveness and control quality."""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

logger = logging.getLogger("raccoon")


@dataclass
class StepResponseMetrics:
    """Metrics from a single step response test."""

    motor_name: str
    target_power: float  # power percentage
    target_velocity: float  # rad/s (estimated/measured)
    direction: str  # "forward" or "reverse"

    # Timing metrics (seconds)
    rise_time: float = 0.0  # Time from 10% to 90% of target
    settling_time: float = 0.0  # Time to stay within settling threshold
    response_time: float = 0.0  # Time to first reach target

    # Quality metrics
    overshoot_percent: float = 0.0  # Peak overshoot as percentage of target
    steady_state_error: float = 0.0  # Average error after settling (rad/s)
    steady_state_error_percent: float = 0.0  # As percentage of target

    # Raw data
    peak_velocity: float = 0.0  # Maximum velocity reached
    samples_collected: int = 0
    test_duration: float = 0.0


@dataclass
class BenchmarkResult:
    """Overall benchmark results for a motor."""

    motor_name: str
    motor_index: int

    # Averaged metrics across all tests
    avg_rise_time: float = 0.0
    avg_settling_time: float = 0.0
    avg_overshoot_percent: float = 0.0
    avg_steady_state_error_percent: float = 0.0

    # Worst case metrics
    max_rise_time: float = 0.0
    max_settling_time: float = 0.0
    max_overshoot_percent: float = 0.0
    max_steady_state_error_percent: float = 0.0

    # Individual test results
    tests: List[StepResponseMetrics] = field(default_factory=list)

    # Overall grade
    grade: str = "?"
    responsiveness_score: float = 0.0  # 0-100


def _analyze_step_response(
    times: List[float],
    velocities: List[float],
    target: float,
    settling_threshold: float = 0.05,  # 5% of target
) -> Dict[str, float]:
    """
    Analyze a step response from time-series data.

    Returns dict with rise_time, settling_time, overshoot_percent, steady_state_error.
    """
    if not times or not velocities or len(times) != len(velocities):
        return {}

    abs_target = abs(target)
    if abs_target < 0.01:
        return {}

    # Find key thresholds
    threshold_10 = 0.10 * abs_target
    threshold_90 = 0.90 * abs_target
    settling_band = settling_threshold * abs_target

    rise_start_time = None
    rise_end_time = None
    first_target_time = None
    peak_velocity = 0.0
    peak_time = 0.0

    # Scan for rise time and peak
    for t, v in zip(times, velocities):
        abs_v = abs(v)

        # Track peak
        if abs_v > peak_velocity:
            peak_velocity = abs_v
            peak_time = t

        # Rise time: 10% threshold
        if rise_start_time is None and abs_v >= threshold_10:
            rise_start_time = t

        # Rise time: 90% threshold
        if rise_end_time is None and abs_v >= threshold_90:
            rise_end_time = t

        # First time reaching target
        if first_target_time is None and abs_v >= abs_target:
            first_target_time = t

    # Calculate rise time
    rise_time = 0.0
    if rise_start_time is not None and rise_end_time is not None:
        rise_time = rise_end_time - rise_start_time

    # Calculate response time
    response_time = first_target_time if first_target_time is not None else times[-1]

    # Calculate overshoot
    overshoot_percent = 0.0
    if peak_velocity > abs_target:
        overshoot_percent = ((peak_velocity - abs_target) / abs_target) * 100.0

    # Find settling time - time after which velocity stays within band
    settling_time = times[-1]  # Default to full duration if never settles
    n = len(times)

    # Work backwards to find last time outside settling band
    for i in range(n - 1, -1, -1):
        error = abs(abs(velocities[i]) - abs_target)
        if error > settling_band:
            # Found exit from settling band
            if i < n - 1:
                settling_time = times[i + 1]
            break
    else:
        # Never left settling band - check if we ever entered it
        for i, (t, v) in enumerate(zip(times, velocities)):
            error = abs(abs(v) - abs_target)
            if error <= settling_band:
                settling_time = t
                break

    # Calculate steady-state error (average of last 20% of samples)
    steady_start_idx = int(0.8 * n)
    if steady_start_idx < n - 1:
        steady_velocities = velocities[steady_start_idx:]
        avg_steady = sum(abs(v) for v in steady_velocities) / len(steady_velocities)
        steady_state_error = abs(avg_steady - abs_target)
        steady_state_error_percent = (steady_state_error / abs_target) * 100.0
    else:
        steady_state_error = 0.0
        steady_state_error_percent = 0.0

    return {
        "rise_time": rise_time,
        "settling_time": settling_time,
        "response_time": response_time,
        "overshoot_percent": overshoot_percent,
        "peak_velocity": peak_velocity,
        "steady_state_error": steady_state_error,
        "steady_state_error_percent": steady_state_error_percent,
    }


def _grade_motor(result: BenchmarkResult) -> tuple[str, float]:
    """
    Grade motor performance based on benchmark results.

    Returns (grade letter, score 0-100).
    """
    # Scoring rubric (lower is better for times/errors, weighted)
    score = 100.0

    # Rise time penalty (target: <0.1s is excellent)
    if result.avg_rise_time > 0.3:
        score -= 25
    elif result.avg_rise_time > 0.2:
        score -= 15
    elif result.avg_rise_time > 0.1:
        score -= 5

    # Settling time penalty (target: <0.3s is excellent)
    if result.avg_settling_time > 1.0:
        score -= 25
    elif result.avg_settling_time > 0.5:
        score -= 15
    elif result.avg_settling_time > 0.3:
        score -= 5

    # Overshoot penalty (target: <10% is excellent)
    if result.avg_overshoot_percent > 30:
        score -= 25
    elif result.avg_overshoot_percent > 20:
        score -= 15
    elif result.avg_overshoot_percent > 10:
        score -= 5

    # Steady-state error penalty (target: <3% is excellent)
    if result.avg_steady_state_error_percent > 10:
        score -= 25
    elif result.avg_steady_state_error_percent > 5:
        score -= 15
    elif result.avg_steady_state_error_percent > 3:
        score -= 5

    score = max(0, min(100, score))

    # Convert to letter grade
    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 70:
        grade = "C"
    elif score >= 60:
        grade = "D"
    else:
        grade = "F"

    return grade, score


def _run_step_response_test(
    motor,
    motor_name: str,
    target_power: float,
    ticks_to_rad: float,
    duration: float,
    sample_rate: float,
) -> StepResponseMetrics:
    """
    Run a single step response test on a motor.

    Commands target_power and records velocity response for duration seconds.
    Velocity is calculated from position changes.
    """
    direction = "forward" if target_power >= 0 else "reverse"

    # Estimate target velocity based on typical motor characteristics
    # At 100% power, motors typically achieve ~15-20 rad/s
    estimated_max_velocity = 18.0  # rad/s at 100% power
    target_velocity = (abs(target_power) / 100.0) * estimated_max_velocity

    metrics = StepResponseMetrics(
        motor_name=motor_name,
        target_power=target_power,
        target_velocity=target_velocity,
        direction=direction,
    )

    times: List[float] = []
    velocities: List[float] = []
    positions: List[float] = []

    sample_interval = 1.0 / sample_rate
    start_time = time.perf_counter()

    try:
        # Get initial position
        last_position = motor.get_position() * ticks_to_rad
        last_sample_time = start_time

        # Command target power
        motor.set_speed(int(target_power))

        # Collect samples
        while True:
            now = time.perf_counter()
            elapsed = now - start_time

            if elapsed >= duration:
                break

            # Sample at specified rate
            if now - last_sample_time >= sample_interval:
                current_position = motor.get_position() * ticks_to_rad
                dt = now - last_sample_time

                # Calculate velocity from position change
                if dt > 0:
                    velocity = (current_position - last_position) / dt
                else:
                    velocity = 0.0

                times.append(elapsed)
                velocities.append(abs(velocity))  # Use absolute velocity for analysis
                positions.append(current_position)

                last_position = current_position
                last_sample_time = now

    finally:
        # Stop motor
        motor.set_speed(0)
        time.sleep(0.1)  # Brief settle time

    # Update target velocity based on actual steady-state if we have data
    if len(velocities) > 10:
        # Use last 20% of samples to estimate actual steady-state velocity
        steady_start = int(0.8 * len(velocities))
        actual_steady_state = sum(velocities[steady_start:]) / len(velocities[steady_start:])
        if actual_steady_state > 0.1:  # Only update if we got meaningful data
            target_velocity = actual_steady_state
            metrics.target_velocity = target_velocity

    # Analyze response
    if times and velocities:
        analysis = _analyze_step_response(times, velocities, target_velocity)

        metrics.rise_time = analysis.get("rise_time", 0.0)
        metrics.settling_time = analysis.get("settling_time", 0.0)
        metrics.response_time = analysis.get("response_time", 0.0)
        metrics.overshoot_percent = analysis.get("overshoot_percent", 0.0)
        metrics.steady_state_error = analysis.get("steady_state_error", 0.0)
        metrics.steady_state_error_percent = analysis.get("steady_state_error_percent", 0.0)
        metrics.peak_velocity = analysis.get("peak_velocity", 0.0)
        metrics.samples_collected = len(times)
        metrics.test_duration = times[-1] if times else 0.0

    return metrics


def _run_motor_benchmark(
    console: Console,
    motor,
    motor_idx: int,
    motor_name: str,
    test_powers: List[float],
    ticks_to_rad: float,
    duration: float,
    sample_rate: float,
) -> BenchmarkResult:
    """Run full benchmark suite on a single motor."""
    result = BenchmarkResult(motor_name=motor_name, motor_index=motor_idx)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Benchmarking {motor_name}...", total=len(test_powers))

        for target_power in test_powers:
            progress.update(task, description=f"Testing {motor_name} @ {target_power:.0f}% power...")

            metrics = _run_step_response_test(
                motor=motor,
                motor_name=motor_name,
                target_power=target_power,
                ticks_to_rad=ticks_to_rad,
                duration=duration,
                sample_rate=sample_rate,
            )
            result.tests.append(metrics)
            progress.advance(task)

            # Brief pause between tests
            time.sleep(0.3)

    # Calculate aggregate metrics
    if result.tests:
        valid_tests = [t for t in result.tests if t.samples_collected > 0]
        if valid_tests:
            result.avg_rise_time = sum(t.rise_time for t in valid_tests) / len(valid_tests)
            result.avg_settling_time = sum(t.settling_time for t in valid_tests) / len(valid_tests)
            result.avg_overshoot_percent = sum(t.overshoot_percent for t in valid_tests) / len(valid_tests)
            result.avg_steady_state_error_percent = sum(t.steady_state_error_percent for t in valid_tests) / len(valid_tests)

            result.max_rise_time = max(t.rise_time for t in valid_tests)
            result.max_settling_time = max(t.settling_time for t in valid_tests)
            result.max_overshoot_percent = max(t.overshoot_percent for t in valid_tests)
            result.max_steady_state_error_percent = max(t.steady_state_error_percent for t in valid_tests)

    # Grade the motor
    result.grade, result.responsiveness_score = _grade_motor(result)

    return result


def _display_benchmark_results(console: Console, results: List[BenchmarkResult]) -> None:
    """Display benchmark results in formatted tables."""
    # Summary table
    summary_table = Table(title="Motor Benchmark Summary", expand=True)
    summary_table.add_column("Motor", style="bold cyan")
    summary_table.add_column("Grade", style="bold", justify="center")
    summary_table.add_column("Score", justify="right")
    summary_table.add_column("Rise Time", justify="right")
    summary_table.add_column("Settling", justify="right")
    summary_table.add_column("Overshoot", justify="right")
    summary_table.add_column("SS Error", justify="right")

    for r in results:
        # Color grade
        grade_colors = {"A": "green", "B": "blue", "C": "yellow", "D": "orange1", "F": "red"}
        grade_color = grade_colors.get(r.grade, "white")
        grade_str = f"[{grade_color}]{r.grade}[/{grade_color}]"

        # Color score
        if r.responsiveness_score >= 80:
            score_style = "green"
        elif r.responsiveness_score >= 60:
            score_style = "yellow"
        else:
            score_style = "red"

        summary_table.add_row(
            r.motor_name,
            grade_str,
            f"[{score_style}]{r.responsiveness_score:.0f}[/{score_style}]",
            f"{r.avg_rise_time * 1000:.1f}ms",
            f"{r.avg_settling_time * 1000:.1f}ms",
            f"{r.avg_overshoot_percent:.1f}%",
            f"{r.avg_steady_state_error_percent:.1f}%",
        )

    console.print()
    console.print(Panel(summary_table, border_style="cyan"))

    # Detailed results for each motor
    for r in results:
        if not r.tests:
            continue

        detail_table = Table(title=f"[cyan]{r.motor_name}[/cyan] - Detailed Results")
        detail_table.add_column("Power %", justify="right")
        detail_table.add_column("Dir", justify="center")
        detail_table.add_column("Rise", justify="right")
        detail_table.add_column("Settling", justify="right")
        detail_table.add_column("Overshoot", justify="right")
        detail_table.add_column("Peak (rad/s)", justify="right")
        detail_table.add_column("SS Err %", justify="right")
        detail_table.add_column("Samples", justify="right", style="dim")

        for t in r.tests:
            detail_table.add_row(
                f"{t.target_power:.0f}",
                t.direction[:3],
                f"{t.rise_time * 1000:.1f}ms",
                f"{t.settling_time * 1000:.1f}ms",
                f"{t.overshoot_percent:.1f}%",
                f"{t.peak_velocity:.2f}",
                f"{t.steady_state_error_percent:.1f}%",
                str(t.samples_collected),
            )

        console.print()
        console.print(detail_table)


def _save_benchmark_csv(
    output_path: Path,
    results: List[BenchmarkResult],
) -> None:
    """Save benchmark results to CSV file."""
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "motor_name",
            "motor_index",
            "target_power_percent",
            "target_velocity_rad_s",
            "direction",
            "rise_time_ms",
            "settling_time_ms",
            "response_time_ms",
            "overshoot_percent",
            "peak_velocity_rad_s",
            "steady_state_error_rad_s",
            "steady_state_error_percent",
            "samples_collected",
            "test_duration_s",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for r in results:
            for t in r.tests:
                writer.writerow({
                    "motor_name": t.motor_name,
                    "motor_index": r.motor_index,
                    "target_power_percent": t.target_power,
                    "target_velocity_rad_s": round(t.target_velocity, 4),
                    "direction": t.direction,
                    "rise_time_ms": round(t.rise_time * 1000, 2),
                    "settling_time_ms": round(t.settling_time * 1000, 2),
                    "response_time_ms": round(t.response_time * 1000, 2),
                    "overshoot_percent": round(t.overshoot_percent, 2),
                    "peak_velocity_rad_s": round(t.peak_velocity, 4),
                    "steady_state_error_rad_s": round(t.steady_state_error, 4),
                    "steady_state_error_percent": round(t.steady_state_error_percent, 2),
                    "samples_collected": t.samples_collected,
                    "test_duration_s": round(t.test_duration, 4),
                })


def _generate_benchmark_plots(console: Console, output_dir: Path, results: List[BenchmarkResult]) -> None:
    """Generate benchmark visualization plots."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        console.print("[yellow]Warning: matplotlib not available, skipping plots.[/yellow]")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # Bar chart comparing motors
    if len(results) > 1:
        fig, axes = plt.subplots(2, 2, figsize=(12, 10))

        motor_names = [r.motor_name for r in results]
        x = range(len(motor_names))

        # Rise time
        ax = axes[0, 0]
        rise_times = [r.avg_rise_time * 1000 for r in results]
        ax.bar(x, rise_times, color="steelblue")
        ax.set_ylabel("Rise Time (ms)")
        ax.set_title("Average Rise Time")
        ax.set_xticks(x)
        ax.set_xticklabels(motor_names, rotation=45, ha="right")

        # Settling time
        ax = axes[0, 1]
        settling_times = [r.avg_settling_time * 1000 for r in results]
        ax.bar(x, settling_times, color="forestgreen")
        ax.set_ylabel("Settling Time (ms)")
        ax.set_title("Average Settling Time")
        ax.set_xticks(x)
        ax.set_xticklabels(motor_names, rotation=45, ha="right")

        # Overshoot
        ax = axes[1, 0]
        overshoots = [r.avg_overshoot_percent for r in results]
        ax.bar(x, overshoots, color="darkorange")
        ax.set_ylabel("Overshoot (%)")
        ax.set_title("Average Overshoot")
        ax.set_xticks(x)
        ax.set_xticklabels(motor_names, rotation=45, ha="right")

        # Scores
        ax = axes[1, 1]
        scores = [r.responsiveness_score for r in results]
        colors = ["green" if s >= 80 else "orange" if s >= 60 else "red" for s in scores]
        ax.bar(x, scores, color=colors)
        ax.set_ylabel("Score")
        ax.set_title("Responsiveness Score")
        ax.set_ylim(0, 100)
        ax.set_xticks(x)
        ax.set_xticklabels(motor_names, rotation=45, ha="right")

        plt.tight_layout()
        plt.savefig(output_dir / "benchmark_comparison.png", dpi=150)
        plt.close()

    console.print(f"[green]✓ Plots saved to {output_dir}[/green]")


def benchmark_motors_local(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    powers: List[float],
    duration: float,
    sample_rate: float,
    output_dir: Optional[str] = None,
) -> None:
    """Run motor benchmark locally (on the Pi itself)."""
    console: Console = ctx.obj["console"]

    # Add project root to Python path for imports
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Import required libraries
    try:
        from src.hardware.defs import Defs
    except ImportError as exc:
        console.print(f"[red]Failed to import Defs: {exc}[/red]")
        console.print("[yellow]Make sure you have run 'raccoon codegen' first.[/yellow]")
        raise SystemExit(1) from exc

    # Determine motor names and get motors from config
    drivetrain_type = config.get("robot", {}).get("drive", {}).get("kinematics", {}).get("type", "")
    definitions = config.get("definitions", {})

    if drivetrain_type == "mecanum":
        motor_names = ["front_left_motor", "front_right_motor", "rear_left_motor", "rear_right_motor"]
    elif drivetrain_type == "differential":
        motor_names = ["left_motor", "right_motor"]
    else:
        console.print(f"[red]Unknown drivetrain type '{drivetrain_type}'[/red]")
        raise SystemExit(1)

    # Get ticks_to_rad calibration values for each motor
    motor_calibrations: Dict[str, float] = {}
    for motor_name in motor_names:
        motor_def = definitions.get(motor_name, {})
        calibration = motor_def.get("calibration", {})
        ticks_to_rad = calibration.get("ticks_to_rad", 0.0041282)  # Default if not calibrated
        motor_calibrations[motor_name] = ticks_to_rad

    console.print(
        Panel(
            f"[bold cyan]Motor PID Benchmark[/bold cyan]\n\n"
            f"Project: [yellow]{config.get('name', 'Unknown')}[/yellow]\n"
            f"Drivetrain: [yellow]{drivetrain_type}[/yellow]\n"
            f"Motors: [yellow]{', '.join(motor_names)}[/yellow]\n"
            f"Test powers: [yellow]{', '.join(f'{p:.0f}%' for p in powers)}[/yellow]\n"
            f"Test duration: [yellow]{duration}s[/yellow]\n"
            f"Sample rate: [yellow]{sample_rate} Hz[/yellow]",
            border_style="cyan",
        )
    )

    # Create defs instance to get motors
    try:
        defs = Defs()
    except Exception as exc:
        console.print(f"[red]Failed to initialize hardware: {exc}[/red]")
        raise SystemExit(1) from exc

    console.print("\n[cyan]Running benchmark tests...[/cyan]\n")

    # Run benchmarks for each motor
    results: List[BenchmarkResult] = []
    try:
        for idx, motor_name in enumerate(motor_names):
            # Get the motor from defs
            motor = getattr(defs, motor_name, None)
            if motor is None:
                console.print(f"[yellow]Warning: Motor '{motor_name}' not found in defs, skipping[/yellow]")
                continue

            ticks_to_rad = motor_calibrations.get(motor_name, 0.0041282)

            result = _run_motor_benchmark(
                console=console,
                motor=motor,
                motor_idx=idx,
                motor_name=motor_name,
                test_powers=powers,
                ticks_to_rad=ticks_to_rad,
                duration=duration,
                sample_rate=sample_rate,
            )
            results.append(result)

            # Brief pause between motors
            time.sleep(0.5)
    finally:
        # Ensure all motors are stopped
        for motor_name in motor_names:
            motor = getattr(defs, motor_name, None)
            if motor is not None:
                try:
                    motor.set_speed(0)
                except Exception:
                    pass

    if not results:
        console.print("[red]No motors were tested.[/red]")
        raise SystemExit(1)

    # Display results
    _display_benchmark_results(console, results)

    # Save CSV
    if output_dir:
        out_path = Path(output_dir)
    else:
        out_path = project_root / "logs" / "motor_benchmark"

    out_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_path / f"benchmark_{timestamp}.csv"
    _save_benchmark_csv(csv_path, results)
    console.print(f"\n[green]✓ Results saved to {csv_path}[/green]")

    # Generate plots
    _generate_benchmark_plots(console, out_path, results)

    # Overall assessment
    console.print()
    all_scores = [r.responsiveness_score for r in results]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0

    if avg_score >= 80:
        console.print(
            Panel(
                f"[bold green]Excellent![/bold green] Average score: {avg_score:.0f}/100\n"
                "Your PID calibration is performing well.",
                border_style="green",
            )
        )
    elif avg_score >= 60:
        console.print(
            Panel(
                f"[bold yellow]Acceptable[/bold yellow] Average score: {avg_score:.0f}/100\n"
                "Consider re-running motor calibration with --aggressive flag for better results.",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                f"[bold red]Needs Improvement[/bold red] Average score: {avg_score:.0f}/100\n"
                "Run 'raccoon calibrate motors --aggressive' to recalibrate.",
                border_style="red",
            )
        )


async def benchmark_motors_remote(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    powers: List[float],
    duration: float,
    sample_rate: float,
    output_dir: Optional[str] = None,
) -> None:
    """Run motor benchmark on the connected Pi."""
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.client.sftp_sync import SyncDirection
    from raccoon.commands.sync_cmd import sync_project_interactive

    # Sync project to Pi before benchmark
    if not sync_project_interactive(project_root, console):
        console.print("[red]Sync failed, cannot run benchmark remotely[/red]")
        raise SystemExit(1)
    console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running motor benchmark for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Build args for benchmark command
    args = ["benchmark"]
    for p in powers:
        args.extend(["--power", str(p)])
    args.extend(["--duration", str(duration)])
    args.extend(["--sample-rate", str(sample_rate)])
    if output_dir:
        args.extend(["--output-dir", output_dir])

    # Start the calibrate benchmark command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.calibrate_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start benchmark on Pi: {e}[/red]")
            raise SystemExit(1)

        # Stream output via WebSocket
        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print()

        final_status = handler.stream_to_console(console)

        exit_code = final_status.get("exit_code", -1)

    console.print()
    console.print("[dim]Syncing benchmark results...[/dim]")
    if sync_project_interactive(project_root, console, direction=SyncDirection.PULL):
        console.print("[green]✓ Benchmark results synced to local project[/green]")
    else:
        console.print("[yellow]Warning: Failed to sync results. Run 'raccoon sync' manually.[/yellow]")

    if exit_code == 0:
        console.print()
        console.print("[green]Benchmark completed on Pi![/green]")
        return

    console.print()
    console.print(f"[red]Benchmark failed with exit code {exit_code}[/red]")
    raise SystemExit(exit_code)
