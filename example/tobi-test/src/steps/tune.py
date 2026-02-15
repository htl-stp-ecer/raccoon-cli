"""
Tune step: runs a single turn trial and writes results to tune_results.json.

The robot's motion_pid_config (set via YAML + codegen) is used directly.
An external Optuna script on the laptop updates raccoon.project.yml between
runs, so each `raccoon run` picks up new PID params via codegen.

Usage in your mission:
    from src.steps.tune import TuneTurn

    class TuningMission(Mission):
        def sequence(self) -> Sequential:
            return seq([TuneTurn(angle=90)])
"""

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from libstp import Step, GenericRobot
from libstp.motion import TurnMotion, TurnConfig


UPDATE_RATE = 1 / 20  # 20 Hz
TRIAL_TIMEOUT = 10.0

# Cost weights
W_TIME = 1.0
W_OVERSHOOT = 80.0
W_ERROR = 30.0
W_OSCILLATION = 2.0

RESULTS_FILE = "tune_results.json"


@dataclass
class TrialResult:
    completed: bool = False
    elapsed_time: float = TRIAL_TIMEOUT
    final_heading_rad: float = 0.0
    final_error_rad: float = 0.0
    max_overshoot_rad: float = 0.0
    oscillation_count: int = 0
    headings: list = field(default_factory=list)
    times: list = field(default_factory=list)


def run_turn(robot: GenericRobot, target_deg: float, speed: float) -> TrialResult:
    """Execute a single turn using the robot's current PID config."""
    target_rad = math.radians(target_deg)
    result = TrialResult()

    turn_cfg = TurnConfig()
    turn_cfg.target_angle_rad = target_rad
    turn_cfg.max_angular_rate = speed

    motion = TurnMotion(robot.drive, robot.odometry, robot.motion_pid_config, turn_cfg)
    motion.start()

    start = time.monotonic()
    last = start - UPDATE_RATE

    while not motion.is_finished():
        now = time.monotonic()
        elapsed = now - start
        if elapsed > TRIAL_TIMEOUT:
            break

        dt = max(now - last, 0.0)
        last = now
        if dt < 1e-4:
            time.sleep(UPDATE_RATE)
            continue

        motion.update(dt)

        result.headings.append(robot.odometry.get_heading())
        result.times.append(elapsed)
        time.sleep(UPDATE_RATE)

    robot.drive.hard_stop()

    result.completed = motion.is_finished()
    result.elapsed_time = time.monotonic() - start
    result.final_heading_rad = robot.odometry.get_heading()
    result.final_error_rad = result.final_heading_rad - target_rad

    # Overshoot
    sign = 1.0 if target_rad > 0 else -1.0
    for h in result.headings:
        overshoot = sign * (h - target_rad)
        if overshoot > 0:
            result.max_overshoot_rad = max(result.max_overshoot_rad, overshoot)

    # Oscillations (zero-crossings of error)
    errors = [h - target_rad for h in result.headings]
    for i in range(1, len(errors)):
        if errors[i] * errors[i - 1] < 0:
            result.oscillation_count += 1

    return result


def compute_cost(result: TrialResult, target_deg: float) -> float:
    if not result.completed:
        # Graduated penalty: closer to target = lower cost (but still bad)
        target_rad = math.radians(target_deg)
        progress = 1.0 - min(abs(result.final_error_rad / target_rad), 1.0) if target_rad else 0.0
        return 500.0 + 500.0 * (1.0 - progress)  # 500–1000 range
    cost = W_TIME * result.elapsed_time
    cost += W_OVERSHOOT * result.max_overshoot_rad ** 2
    cost += W_ERROR * result.final_error_rad ** 2
    cost += W_OSCILLATION * result.oscillation_count
    return cost


class TuneTurn(Step):
    """
    Run a single turn trial and write results to tune_results.json.

    Uses the robot's current motion_pid_config (from YAML codegen).
    Designed to be called repeatedly by an external Optuna script
    that modifies raccoon.project.yml between runs.
    """

    def __init__(self, angle: float = 90.0, speed: float = 1.0):
        super().__init__()
        self.angle = angle
        self.speed = speed

    async def _execute_step(self, robot: GenericRobot) -> None:
        cfg = robot.motion_pid_config
        self.info(
            f"Turn trial: {self.angle}° | "
            f"kp={cfg.heading.kp:.4f} ki={cfg.heading.ki:.4f} "
            f"kd={cfg.heading.kd:.4f} lpf={cfg.heading.derivative_lpf_alpha:.4f}"
        )

        result = run_turn(robot, self.angle, self.speed)
        cost = compute_cost(result, self.angle)

        status = "OK" if result.completed else "TIMEOUT"
        self.info(
            f"{status} | {result.elapsed_time:.2f}s "
            f"err={math.degrees(abs(result.final_error_rad)):.2f}° "
            f"overshoot={math.degrees(result.max_overshoot_rad):.1f}° "
            f"osc={result.oscillation_count} cost={cost:.3f}"
        )

        output = {
            "completed": result.completed,
            "cost": cost,
            "elapsed_time": result.elapsed_time,
            "final_error_deg": math.degrees(result.final_error_rad),
            "max_overshoot_deg": math.degrees(result.max_overshoot_rad),
            "oscillation_count": result.oscillation_count,
            "headings_deg": [math.degrees(h) for h in result.headings],
            "times": result.times,
        }

        Path(RESULTS_FILE).write_text(json.dumps(output, indent=2))
        self.info(f"Results written to {RESULTS_FILE}")
