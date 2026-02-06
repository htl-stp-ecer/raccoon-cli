from libstp import dsl, UIStep, GenericRobot

from src.hardware.thresholded_sensor import ThresholdedSensor, DEFAULT_THRESHOLD_FRACTION
from src.steps.thresholded_sensor.screens.thresholded_confirm_screen import ThresholdedConfirmScreen
from src.steps.thresholded_sensor.screens.thresholded_sampling_screen import ThresholdedSamplingScreen


@dsl(hidden=True)
class CalibrateThresholdedStep(UIStep):
    def __init__(
        self,
        spike_sensor: ThresholdedSensor,
        duration: float = 5.0,
        threshold_fraction: float = DEFAULT_THRESHOLD_FRACTION,
    ):
        super().__init__()
        self.spike_sensor = spike_sensor
        self.duration = duration
        self.threshold_fraction = threshold_fraction

    async def _execute_step(self, robot: "GenericRobot") -> None:
        while True:
            # Phase 1: sample while user rolls object underneath
            screen = ThresholdedSamplingScreen(sensor_port=self.spike_sensor.port)
            samples = await self.run_with_ui(
                screen,
                self.spike_sensor.sample(self.duration),
            )

            if len(samples) < 20:
                self.warn("Too few samples collected, retrying")
                continue

            # Phase 2: analyze samples for spike
            try:
                self.spike_sensor.calibrate(samples, self.threshold_fraction)
            except ValueError as e:
                self.warn(str(e))
                continue

            # Phase 3: confirm results
            result = await self.show(
                ThresholdedConfirmScreen(
                    baseline=self.spike_sensor.baseline,
                    spike_amplitude=self.spike_sensor.spike_amplitude,
                    threshold=self.spike_sensor.threshold,
                    samples=samples,
                )
            )

            if result.confirmed:
                self.spike_sensor.apply_calibration(
                    result.baseline, result.threshold
                )
                return
            # retry → loop continues




@dsl
def calibrate_threshold_sensor(
    spike_sensor: ThresholdedSensor,
    duration: float = 5.0,
    threshold_fraction: float = 0.4,
) -> CalibrateThresholdedStep:
    """Calibrate a spike sensor by sampling while an object rolls underneath.

    Shows a UI with live sensor values during sampling, then presents
    the detected baseline and spike threshold for confirmation.

    Args:
        spike_sensor: The SpikeSensor to calibrate.
        duration: How long to sample in seconds.
        threshold_fraction: Fraction of spike amplitude to use as trigger (0.0-1.0).
    """
    return CalibrateThresholdedStep(
        spike_sensor=spike_sensor,
        duration=duration,
        threshold_fraction=threshold_fraction,
    )