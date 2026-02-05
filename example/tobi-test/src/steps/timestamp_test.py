import asyncio
import math
import time

from libstp import Sequential, seq, Turn, dsl
from libstp.foundation import ChassisVelocity, info
from libstp.motion import TurnConfig, DriveStraightConfig
from libstp.step.motion import Drive
from libstp.sensor_ir import IRSensor
from libstp.step import Step
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from libstp.robot.api import GenericRobot


class SurfaceColor(Enum):
    BLACK = 0
    WHITE = 1


@dsl(hidden=True)
class TimingBasedLineUp(Step):
    def __init__(self, left_sensor: IRSensor, right_sensor: IRSensor,
                 target: SurfaceColor = SurfaceColor.BLACK,
                 forward_speed: float = 1.0,
                 detection_threshold: float = 0.9):
        super().__init__()
        self.left_sensor = left_sensor
        self.right_sensor = right_sensor
        self.target = target
        self.forward_speed = forward_speed
        self.threshold = detection_threshold
        self.distance_between_hits_m: float = 0.0  # Distance traveled between sensor hits
        self.result = (None, 0.0)  # (first_sensor: str, distance_between_hits_m: float)

    def _get_confidences(self):
        if self.target == SurfaceColor.BLACK:
            return self.left_sensor.probabilityOfBlack(), self.right_sensor.probabilityOfBlack()
        else:
            return self.left_sensor.probabilityOfWhite(), self.right_sensor.probabilityOfWhite()

    async def _execute_step(self, robot: "GenericRobot") -> None:
        left_triggered = False
        right_triggered = False
        t_first = None
        first_sensor = None
        first_hit_distance: float = 0.0

        # Reset odometry to track distance from start
        robot.odometry.reset()

        robot.drive.set_velocity(ChassisVelocity(self.forward_speed, 0.0, 0.0))

        update_rate = 1 / 100  # 100 Hz
        last_time = asyncio.get_event_loop().time() - update_rate

        while not (left_triggered and right_triggered):
            current_time = asyncio.get_event_loop().time()
            delta_time = max(current_time - last_time, 0.0)
            last_time = current_time

            robot.odometry.update(delta_time)
            robot.drive.update(delta_time)

            left_conf, right_conf = self._get_confidences()
            now = time.monotonic()

            # Use get_distance_from_origin() - avoids Pose.position crash
            distance_info = robot.odometry.get_distance_from_origin()
            current_distance = distance_info.forward
            # info(f"Left conf: {left_conf:.3f}, Right conf: {right_conf:.3f}, Distance: {robot.odometry.get_distance_from_origin()}")

            if not left_triggered and left_conf >= self.threshold:
                left_triggered = True
                if t_first is None:
                    t_first = now
                    first_sensor = "left"
                    first_hit_distance = current_distance
                    # info("Left sensor hit line at t = 0.000s")
                else:
                    dt = now - t_first
                    self.distance_between_hits_m = abs(current_distance - first_hit_distance)
                    # info(f"Left sensor hit line at t = {dt:.3f}s, distance = {self.distance_between_hits_m * 100:.2f}cm")
                    robot.drive.hard_stop()
                    break

            if not right_triggered and right_conf >= self.threshold:
                right_triggered = True
                if t_first is None:
                    t_first = now
                    first_sensor = "right"
                    first_hit_distance = current_distance
                    # info("Right sensor hit line at t = 0.000s")
                else:
                    dt = now - t_first
                    self.distance_between_hits_m = abs(current_distance - first_hit_distance)
                    # info(f"Right sensor hit line at t = {dt:.3f}s, distance = {self.distance_between_hits_m * 100:.2f}cm")
                    robot.drive.hard_stop()
                    break

            await asyncio.sleep(update_rate)

        robot.drive.hard_stop()
        # info(f"Distance between sensor hits: {self.distance_between_hits_m * 100:.2f}cm")
        self.results = (first_sensor, self.distance_between_hits_m)


@dsl(hidden=True)
class ComputeTimingBasedAngle(Turn):
    wheeleBase = 0.065  # m; Todo: Use real value from robot config

    def __init__(self, step: TimingBasedLineUp):
        config = TurnConfig()
        config.max_angular_rate = 1.0
        super().__init__(config)
        self.step = step

    async def _execute_step(self, robot: "GenericRobot") -> None:
        res = self.step.results[1] * 1.025  # Todo: Configurable?
        self.config.target_angle_rad = math.atan(res / ComputeTimingBasedAngle.wheeleBase)
        if self.step.results[0] == "right":
            self.config.target_angle_rad = -self.config.target_angle_rad
        await super()._execute_step(robot)


class DriveStuffBackward(Drive):
    wheeleBase = 0.065  # m; Todo: Use real value from robot config

    def __init__(self, step: TimingBasedLineUp):
        config = DriveStraightConfig()
        config.max_speed_mps = 1.0
        super().__init__(config)
        self.step = step

    async def _execute_step(self, robot: "GenericRobot") -> None:
        res = self.step.results[1] * 1.025  # Todo: Configurable?
        target_angle_rad = math.atan(res / ComputeTimingBasedAngle.wheeleBase)
        m = 0.087 - math.cos(target_angle_rad) * (0.087 - (ComputeTimingBasedAngle.wheeleBase / 2))
        self.config.distance_m = -m  # Negative distance for backwards
        self.info(f"Driving backward for {self.config.distance_m * 100:.2f}cm to adjust for lineup")
        await super()._execute_step(robot)


@dsl(hidden=True)
def lineup(left_sensor: IRSensor, right_sensor: IRSensor, target: SurfaceColor = SurfaceColor.BLACK) -> Sequential:
    step = TimingBasedLineUp(left_sensor, right_sensor, target)

    return seq([
        step,
        ComputeTimingBasedAngle(step),
        DriveStuffBackward(step)
    ])
