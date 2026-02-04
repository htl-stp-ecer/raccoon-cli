import asyncio
import time
from collections import deque
from libstp.foundation import ChassisVelocity, info
from libstp.sensor_ir import IRSensor
from libstp.step import Step
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from libstp.robot.api import GenericRobot


class SurfaceColor(Enum):
    BLACK = 0
    WHITE = 1


class ReverseAlignOnEdge(Step):
    def __init__(self, left_sensor: IRSensor, right_sensor: IRSensor,
                 forward_speed: float = 0.25,
                 reverse_speed: float = -0.05,
                 trigger_threshold: float = 0.3,
                 exit_threshold: float = 0.2,
                 sensor_avg_window: int = 3):
        super().__init__()
        self.left_sensor = left_sensor
        self.right_sensor = right_sensor
        self.forward_speed = forward_speed
        self.reverse_speed = reverse_speed
        self.trigger_threshold = trigger_threshold
        self.exit_threshold = exit_threshold
        self.left_buffer = deque(maxlen=sensor_avg_window)
        self.right_buffer = deque(maxlen=sensor_avg_window)

    def _avg_confidence(self) -> tuple[float, float]:
        self.left_buffer.append(self.left_sensor.probabilityOfBlack())
        self.right_buffer.append(self.right_sensor.probabilityOfBlack())
        return sum(self.left_buffer) / len(self.left_buffer), sum(self.right_buffer) / len(self.right_buffer)

    async def _drive_until_trigger(self, robot: "GenericRobot") -> None:
        last_time = asyncio.get_event_loop().time()

        while True:
            now = asyncio.get_event_loop().time()
            dt = now - last_time
            last_time = now

            robot.drive.set_velocity(ChassisVelocity(self.forward_speed, 0.0, 0.0))
            robot.drive.update(dt)

            left_avg, right_avg = self._avg_confidence()
            info(f"Forward: L={left_avg:.2f}, R={right_avg:.2f}")
            if left_avg >= self.trigger_threshold or right_avg >= self.trigger_threshold:
                break
            await asyncio.sleep(0.01)

        await asyncio.sleep(0.1)
        robot.drive.hard_stop()

    async def _reverse_until_exit(self, robot: "GenericRobot") -> None:
        last_time = asyncio.get_event_loop().time()

        while True:
            now = asyncio.get_event_loop().time()
            dt = now - last_time
            last_time = now

            robot.drive.set_velocity(ChassisVelocity(self.reverse_speed, 0.0, 0.0))
            robot.drive.update(dt)

            left_avg, right_avg = self._avg_confidence()
            info(f"Reverse: L={left_avg:.2f}, R={right_avg:.2f}")
            if left_avg <= self.exit_threshold and right_avg <= self.exit_threshold:
                break
            await asyncio.sleep(0.01)

        robot.drive.hard_stop()

    async def _execute_step(self, robot: "GenericRobot") -> None:
        await self._drive_until_trigger(robot)
        #await self._reverse_until_exit(robot)
        info("Alignment complete: robot centered on line edge")