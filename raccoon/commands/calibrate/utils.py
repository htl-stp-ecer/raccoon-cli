"""Shared utilities for calibration commands."""

from __future__ import annotations

import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import yaml


def wait_for_sensor_trigger(sensor, timeout: float = 30.0) -> bool:
    """
    Wait for the hall effect sensor to trigger (go from False to True).

    Returns True if triggered, False if timeout.
    """
    start_time = time.time()
    # Wait for sensor to go low first (if it's high)
    while sensor.read():
        if time.time() - start_time > timeout:
            return False
        time.sleep(0.001)

    # Wait for sensor to go high (magnet detected)
    while not sensor.read():
        if time.time() - start_time > timeout:
            return False
        time.sleep(0.001)

    return True


def count_rotations_fast(
    sensor,
    motor,
    num_rotations: int,
    magnets_per_rotation: int,
    timeout: float = 30.0,
) -> dict:
    """
    Count rotations using hall effect sensor with timing analysis.

    Returns dict with:
        - elapsed: total time in seconds
        - bemf_delta: encoder ticks moved
        - trigger_times: list of timestamps for each magnet detection
        - intervals: list of intervals between triggers
        - anomalies: list of detected anomalies
    """
    total_triggers = num_rotations * magnets_per_rotation
    trigger_count = 0
    trigger_times = []

    start_time = time.perf_counter()  # High-resolution timer
    start_bemf = motor.get_position()
    last_sensor_state = sensor.read()

    # Tight polling loop - no sleep for maximum speed
    while trigger_count < total_triggers:
        now = time.perf_counter()
        if now - start_time > timeout:
            break

        current_state = sensor.read()

        # Detect rising edge (False -> True)
        if current_state and not last_sensor_state:
            trigger_times.append(now - start_time)
            trigger_count += 1

        last_sensor_state = current_state

    end_time = time.perf_counter()
    end_bemf = motor.get_position()

    elapsed = end_time - start_time
    bemf_delta = abs(end_bemf - start_bemf)

    # Calculate intervals between triggers
    intervals = []
    if len(trigger_times) > 1:
        for i in range(1, len(trigger_times)):
            intervals.append(trigger_times[i] - trigger_times[i - 1])

    # Detect anomalies in periodicity
    anomalies = []
    if len(intervals) >= 3:
        median_interval = sorted(intervals)[len(intervals) // 2]
        for i, interval in enumerate(intervals):
            # Flag if interval deviates more than 30% from median
            deviation = abs(interval - median_interval) / median_interval if median_interval > 0 else 0
            if deviation > 0.30:
                anomalies.append({
                    "trigger_index": i + 1,
                    "interval": interval,
                    "expected": median_interval,
                    "deviation_percent": round(deviation * 100, 1),
                })

    return {
        "elapsed": elapsed,
        "bemf_delta": bemf_delta,
        "trigger_times": trigger_times,
        "intervals": intervals,
        "anomalies": anomalies,
        "trigger_count": trigger_count,
    }


def find_motor_by_port(config: Dict[str, Any], port: int) -> str | None:
    """Find motor name by port number in config definitions."""
    definitions = config.get("definitions", {})
    for name, definition in definitions.items():
        if definition.get("type") == "Motor" and definition.get("port") == port:
            return name
    return None


def save_project_config(config: Dict[str, Any], project_root: "Path") -> None:
    """Save project configuration to YAML file."""
    config_path = project_root / "raccoon.project.yml"
    with open(config_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False, default_flow_style=False)
