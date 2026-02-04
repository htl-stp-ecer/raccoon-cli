"""
Line up on black or white lines using two IR sensors.

This module provides steps for aligning the robot on lines using differential
wheel control based on sensor feedback.
"""
import asyncio
import time
from dataclasses import dataclass
from libstp.foundation import ChassisVelocity, PIDController
from libstp.foundation import info
from libstp.sensor_ir import IRSensor
from libstp.step import Sequential, seq
from typing import TYPE_CHECKING

from libstp import SurfaceColor, drive_until_black, drive_until_white, wait
from libstp import Step, SimulationStep, SimulationStepDelta, dsl

if TYPE_CHECKING:
    from libstp.robot.api import GenericRobot

def slew(prev: float, target: float, max_rate: float, dt: float) -> float:
    step = max_rate * dt
    if target > prev + step:
        return prev + step
    if target < prev - step:
        return prev - step
    return target

def ema(prev: float, x: float, alpha: float) -> float:
    return prev + alpha * (x - prev)

class Lineup(Step):
    def __init__(
        self,
        left_sensor: IRSensor,
        right_sensor: IRSensor,
        speed: float = 0.15,
        setpoint: float = 0.5,
        bandwidth: float = 0.2,
        target: SurfaceColor = SurfaceColor.BLACK,
    ):
        super().__init__()
        self.left_sensor = left_sensor
        self.right_sensor = right_sensor
        self.speed = speed
        self.setpoint = setpoint
        self.dead_zone = bandwidth / 2
        self.target = target

    def _get_confidences(self) -> tuple[float, float]:
        """Get left and right sensor confidences for the target color."""
        if self.target == SurfaceColor.WHITE:
            left_conf = self.left_sensor.probabilityOfWhite()
            right_conf = self.right_sensor.probabilityOfWhite()
        else:
            left_conf = self.left_sensor.probabilityOfBlack()
            right_conf = self.right_sensor.probabilityOfBlack()
        return left_conf, right_conf

    def _is_aligned(self, left_conf: float, right_conf: float) -> bool:
        """Check if both sensors are at the edge (near confidence_threshold)."""
        return (abs(left_conf - self.setpoint) <= self.dead_zone and
                abs(right_conf - self.setpoint) <= self.dead_zone)


    async def _execute_step(self, robot: "GenericRobot") -> None:
        update_rate = 1 / 100
        last_time = asyncio.get_event_loop().time() - update_rate

        while True:
            # print current confidences

            current_time = asyncio.get_event_loop().time()
            delta_time = max(current_time - last_time, 0.0)
            last_time = current_time

            if delta_time < 1e-4:
                await asyncio.sleep(update_rate)
                continue

            left_conf, right_conf = self._get_confidences()
            info(f"LineUp: L={left_conf:.2f} R={right_conf:.2f}")
            # Check if aligned
            if self._is_aligned(left_conf, right_conf):
                robot.drive.hard_stop()
                return

            now = time.monotonic()

            left_error = self.setpoint - left_conf
            right_error = self.setpoint - right_conf

            v_l = left_error * self.speed #left_pid.calculate(left_error)
            v_r = right_error * self.speed #right_pid.calculate(right_error)
            L = 0.12

            vx_target = (v_r + v_l) / 2
            wz_target = (v_r - v_l) / L


            velocity = ChassisVelocity(vx_target, 0.0, wz_target)
            robot.drive.set_velocity(velocity)
            robot.drive.update(delta_time)

            await asyncio.sleep(update_rate)


@dsl(tags=["motion", "lineup"])
def edge_lineup_on_black(
        left_sensor: IRSensor,
        right_sensor: IRSensor,
        approach_speed: float = 0.5,
        confidence_threshold: float = 0.1,

) -> Sequential:
    return seq([
        drive_until_black(
            [left_sensor, right_sensor],
            approach_speed,
            confidence_threshold=confidence_threshold,
        ),
        Lineup(
            left_sensor=left_sensor,
            right_sensor=right_sensor,
        ),
    ])
