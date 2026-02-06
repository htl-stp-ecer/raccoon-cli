"""
Spike detection on analog sensors.

Provides SpikeSensor (extends AnalogSensor) and steps for:
- Calibrating by sampling while an object rolls underneath the sensor
- Waiting until a matching spike is detected at runtime
"""
import asyncio
import statistics
from typing import TYPE_CHECKING, List, Optional

from libstp import AnalogSensor

if TYPE_CHECKING:
    pass

SAMPLE_INTERVAL = 0.01  # ~100 Hz
EMA_ALPHA = 0.9  # smoothing factor for runtime detection
DEFAULT_THRESHOLD_FRACTION = 0.4  # fraction of spike amplitude used as trigger
MIN_SPIKE_READINGS = 3  # consecutive spike readings required to confirm


class ThresholdedSensor(AnalogSensor):
    """AnalogSensor with calibratable spike detection.

    Extends AnalogSensor to detect transient spikes (e.g. an object rolling
    underneath). Calibrate first by sampling while an object passes, then use
    wait_for_spike() to block until a similar event occurs.

    Can be used anywhere an AnalogSensor is expected.
    """

    def __init__(self, port: int):
        super().__init__(port)
        self._baseline: Optional[float] = None
        self._spike_peak: Optional[float] = None
        self._threshold: Optional[float] = None

    @property
    def is_calibrated(self) -> bool:
        return self._baseline is not None and self._threshold is not None

    @property
    def baseline(self) -> float:
        assert self.is_calibrated, "Not calibrated"
        return self._baseline

    @property
    def threshold(self) -> float:
        assert self.is_calibrated, "Not calibrated"
        return self._threshold

    @property
    def spike_amplitude(self) -> float:
        assert self.is_calibrated, "Not calibrated"
        return self._spike_peak

    def read_float(self) -> float:
        return float(self.read())

    async def sample(self, duration: float) -> List[float]:
        """Collect sensor readings at ~100 Hz for the given duration."""
        samples: List[float] = []
        loop = asyncio.get_event_loop()
        t_end = loop.time() + duration
        while loop.time() < t_end:
            samples.append(self.read_float())
            await asyncio.sleep(SAMPLE_INTERVAL)
        return samples

    def calibrate(
            self,
            samples: List[float],
            threshold_fraction: float = DEFAULT_THRESHOLD_FRACTION,
    ) -> None:
        """Analyze calibration samples to extract baseline and spike threshold.

        Uses the median as a robust baseline estimate (unaffected by the brief
        spike). The spike amplitude is the maximum deviation from baseline.
        """
        if len(samples) < 20:
            raise ValueError(f"Too few samples ({len(samples)}), need at least 20")

        # Median is robust to the brief spike outliers
        baseline = statistics.median(samples)

        # Peak deviation in either direction
        max_deviation = max(abs(s - baseline) for s in samples)

        if max_deviation < 5:
            raise ValueError(
                f"No significant spike detected (peak deviation: {max_deviation:.1f})"
            )

        self._baseline = baseline
        self._spike_peak = max_deviation
        self._threshold = max_deviation * threshold_fraction

    def apply_calibration(self, baseline: float, threshold: float) -> None:
        """Manually set calibration values."""
        self._baseline = baseline
        self._threshold = threshold
        self._spike_peak = threshold / DEFAULT_THRESHOLD_FRACTION

    def is_spike(self, value: float) -> bool:
        """Check if a reading constitutes a spike relative to baseline."""
        assert self.is_calibrated
        return abs(value - self._baseline) >= self._threshold

    async def wait_for_spike(self) -> float:
        """Block until a spike is detected. Returns the peak filtered reading.

        Uses EMA smoothing and requires MIN_SPIKE_READINGS consecutive spike
        readings to confirm (debounce). If the sensor starts in a spiked state,
        waits for it to clear first before looking for a new spike.
        """
        assert self.is_calibrated, "Must calibrate before detecting spikes"
        filtered = self.read_float()
        spike_count = 0
        was_clear = not self.is_spike(filtered)

        while True:
            raw = self.read_float()
            filtered += EMA_ALPHA * (raw - filtered)

            if not self.is_spike(filtered):
                was_clear = True
                spike_count = 0
            elif was_clear:
                spike_count += 1
                if spike_count >= MIN_SPIKE_READINGS:
                    return filtered

            await asyncio.sleep(SAMPLE_INTERVAL)
