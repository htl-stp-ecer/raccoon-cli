from libstp import dsl, GenericRobot, Step

from src.hardware.thresholded_sensor import ThresholdedSensor


@dsl(hidden=True)
class WaitForThresholdStep(Step):
    def __init__(self, spike_sensor: ThresholdedSensor):
        super().__init__()
        self.spike_sensor = spike_sensor

    async def _execute_step(self, robot: "GenericRobot") -> None:
        await self.spike_sensor.wait_for_spike()


@dsl
def wait_for_threshold(spike_sensor: ThresholdedSensor) -> WaitForThresholdStep:
    """Wait until the spike sensor detects a spike matching the calibrated profile.

    Blocks until the sensor reading deviates from baseline by more than the
    calibrated threshold. Must be calibrated first via calibrate_spike_sensor().

    Args:
        spike_sensor: A calibrated SpikeSensor instance.
    """
    return WaitForThresholdStep(spike_sensor)
