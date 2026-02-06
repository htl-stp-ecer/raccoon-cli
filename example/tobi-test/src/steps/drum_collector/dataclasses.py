from dataclasses import dataclass


@dataclass
class DrumCalibrationResult:
    confirmed: bool
    blocked_threshold: float
    pocket_threshold: float
