"""BEMF calibration calculations.

Computes bemfScale and bemfOffset values to linearize BEMF readings
across different motor speeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class CalibrationPoint:
    """A single data point from RPM calibration."""
    power_percent: int
    time_seconds: float
    revolutions: int
    bemf_ticks: int
    rpm: float = 0.0
    tick_rate: float = 0.0
    ticks_per_rev: float = 0.0


@dataclass
class BEMFCalibrationResult:
    """Result of BEMF calibration fitting."""
    bemf_scale: float
    bemf_offset: float
    ticks_per_revolution: float
    r_squared: float
    data_points_used: int


def calculate_derived_values(data: List[CalibrationPoint]) -> List[CalibrationPoint]:
    """Calculate RPM, tick_rate, and ticks_per_rev for each data point."""
    for point in data:
        if point.time_seconds > 0:
            point.rpm = (point.revolutions / point.time_seconds) * 60
            point.tick_rate = point.bemf_ticks / point.time_seconds
            point.ticks_per_rev = point.bemf_ticks / point.revolutions
    return data


def _mean(values: List[float]) -> float:
    """Calculate mean of a list of values."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _polyfit_linear(x: List[float], y: List[float]) -> Tuple[float, float]:
    """
    Simple linear least-squares fit: y = a*x + b.

    Returns (a, b) - slope and intercept.
    """
    n = len(x)
    if n < 2:
        raise ValueError("Need at least 2 points for linear fit")

    mean_x = _mean(x)
    mean_y = _mean(y)

    # Calculate slope
    numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    denominator = sum((xi - mean_x) ** 2 for xi in x)

    if abs(denominator) < 1e-10:
        raise ValueError("Cannot fit - all x values are identical")

    a = numerator / denominator
    b = mean_y - a * mean_x

    return a, b


def _calculate_r_squared(x: List[float], y: List[float], a: float, b: float) -> float:
    """Calculate R² (coefficient of determination) for the linear fit."""
    mean_y = _mean(y)

    ss_res = sum((yi - (a * xi + b)) ** 2 for xi, yi in zip(x, y))
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)

    if ss_tot < 1e-10:
        return 0.0

    return 1 - (ss_res / ss_tot)


def fit_bemf_calibration(
    data: List[CalibrationPoint],
    min_power: int = 20,
    target_ticks_per_rev: Optional[float] = None,
    sample_rate_hz: float = 200.0,
) -> BEMFCalibrationResult:
    """
    Fit BEMF calibration parameters from collected data.

    The goal is to make ticks_per_rev constant across all speeds.
    At high speeds, BEMF is reliable, so we use that as the target.

    Args:
        data: List of calibration points with derived values calculated
        min_power: Minimum power level (%) to include in fit
        target_ticks_per_rev: Target ticks/rev (auto-detected if None)
        sample_rate_hz: BEMF sampling rate (default 200 Hz = 5ms)

    Returns:
        BEMFCalibrationResult with scale, offset, and fit quality metrics

    The firmware applies calibration as:
        calibrated_tick = raw_tick * scale + offset (per sample)
    """
    # Filter out unreliable low-power data
    reliable_data = [p for p in data if p.power_percent >= min_power and p.rpm > 0]

    if len(reliable_data) < 2:
        raise ValueError(f"Need at least 2 data points with power >= {min_power}%")

    # Use high-power data (>= 50%) to determine target ticks_per_rev
    high_power_data = [p for p in reliable_data if p.power_percent >= 50]
    if not high_power_data:
        # Use top 3 data points by power if no high power data
        high_power_data = sorted(reliable_data, key=lambda p: p.power_percent)[-3:]

    if target_ticks_per_rev is None:
        target_ticks_per_rev = _mean([p.ticks_per_rev for p in high_power_data])

    # Extract RPM and tick_rate arrays
    rpms = [p.rpm for p in reliable_data]
    tick_rates = [p.tick_rate for p in reliable_data]

    # Fit linear: tick_rate = a * rpm + b
    a, b = _polyfit_linear(rpms, tick_rates)
    r_squared = _calculate_r_squared(rpms, tick_rates, a, b)

    # The ideal tick_rate for a given RPM would be:
    #   ideal_tick_rate = rpm * (target_ticks_per_rev / 60)
    #
    # We have: actual_tick_rate = a * rpm + b
    # We want: calibrated_tick_rate = rpm * (target_ticks_per_rev / 60)
    #
    # If we apply scale and offset:
    #   calibrated = actual * scale + offset_per_second
    #   rpm * (target/60) = (a * rpm + b) * scale + offset_per_second
    #
    # This should hold for all RPMs, so:
    #   target/60 = a * scale  =>  scale = target / (60 * a)
    #   0 = b * scale + offset_per_second  =>  offset_per_second = -b * scale

    target_slope = target_ticks_per_rev / 60.0  # ticks per second per RPM

    if abs(a) < 0.001:
        # Slope too small, use default values
        scale = 1.0
        offset = 0.0
    else:
        scale = target_slope / a
        offset_per_second = -b * scale
        offset = offset_per_second / sample_rate_hz  # Convert to per-sample

    return BEMFCalibrationResult(
        bemf_scale=scale,
        bemf_offset=offset,
        ticks_per_revolution=target_ticks_per_rev,
        r_squared=r_squared,
        data_points_used=len(reliable_data),
    )


def rpm_data_to_calibration_points(rpm_data: List[dict]) -> List[CalibrationPoint]:
    """
    Convert RPM calibration data (from CSV/dict) to CalibrationPoint objects.

    Expected dict keys: power_percent, time_seconds, rpm, bemf_ticks, ticks_per_revolution
    """
    points = []
    for row in rpm_data:
        # Infer revolutions from ticks_per_revolution if available
        ticks = row.get("bemf_ticks", 0)
        ticks_per_rev = row.get("ticks_per_revolution", 0)

        if ticks_per_rev > 0:
            revolutions = int(round(ticks / ticks_per_rev))
        else:
            revolutions = 5  # Default assumption

        point = CalibrationPoint(
            power_percent=int(row.get("power_percent", 0)),
            time_seconds=float(row.get("time_seconds", 0)),
            revolutions=revolutions,
            bemf_ticks=int(row.get("bemf_ticks", 0)),
            rpm=float(row.get("rpm", 0)),
            tick_rate=float(row.get("bemf_ticks", 0)) / float(row.get("time_seconds", 1)),
            ticks_per_rev=float(row.get("ticks_per_revolution", 0)),
        )
        points.append(point)

    return points
