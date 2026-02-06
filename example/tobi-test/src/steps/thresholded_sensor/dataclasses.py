from dataclasses import dataclass


@dataclass
class ThresholdedCalibrationResult:
    confirmed: bool
    baseline: float
    threshold: float
