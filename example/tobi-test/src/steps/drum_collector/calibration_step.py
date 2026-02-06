from libstp import GenericRobot, dsl
from libstp.ui.step import UIStep

from src.service.drum_motor_service import DrumMotorService

from .dataclasses import DrumCalibrationResult
from .screens import DrumSamplingScreen, DrumConfirmScreen


@dsl(hidden=True)
class DrumCollectorCalibrationStep(UIStep):
    def __init__(self, calibration_time: float = 5.0, motor_speed: float = 0.2):
        super().__init__()
        self.calibration_time = calibration_time
        self.motor_speed = motor_speed

    async def _execute_step(self, robot: "GenericRobot") -> None:
        service = robot.get_service(DrumMotorService)

        while True:
            # Phase 1: sample while showing progress screen
            sampling_screen = DrumSamplingScreen(sensor_port=service.light_sensor.port)
            samples = await self.run_with_ui(
                sampling_screen,
                service.sample(self.calibration_time, self.motor_speed),
            )

            if len(samples) < 20:
                self.warn("Too few samples collected, retrying")
                continue

            # Phase 2: cluster into pocket / blocked
            pocket, blocked = service.cluster(samples)

            # Phase 3: confirm
            result = await self.show(DrumConfirmScreen(
                blocked_threshold=blocked,
                pocket_threshold=pocket,
                collected_values=samples,
            ))

            if result.confirmed:
                service.apply_calibration(result.blocked_threshold, result.pocket_threshold)
                return
            # retry → loop continues


@dsl()
def calibrate_drum_collector(
    calibration_time: float = 5.0,
    motor_speed: float = 0.7,
) -> DrumCollectorCalibrationStep:
    """
    Calibrate the drum collector by spinning the motor and sampling the light sensor.

    Detects two thresholds (blocked vs pocket) using KMeans clustering on collected
    sensor values. Motor/sensor refs are resolved from the robot via DrumMotorService.

    Args:
        calibration_time: How long to spin and sample in seconds, default 5.0.
        motor_speed: Motor speed as a fraction (0.0-1.0), default 0.2.
    """
    return DrumCollectorCalibrationStep(
        calibration_time=calibration_time,
        motor_speed=motor_speed,
    )
