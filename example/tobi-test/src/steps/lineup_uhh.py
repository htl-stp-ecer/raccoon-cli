import asyncio

from libstp import Step, IRSensor, dsl
from libstp.foundation import ChassisVelocity, info


@dsl
class LineupUHH(Step):

    def __init__(self,
                 left_sensor: IRSensor,
                 right_sensor: IRSensor,
                 ):
        super().__init__()
        self.left_sensor = left_sensor
        self.right_sensor = right_sensor

    def _get_velocity(self, left_conf, right_conf) -> ChassisVelocity:
        left_speed = 0
        right_speed = 0
        is_left_black = left_conf > 0.5
        is_right_black = right_conf > 0.5
        if is_left_black and not is_right_black:
            left_speed = -0.01
            right_speed = 0.01
        elif not is_left_black and is_right_black:
            left_speed = 0.01
            right_speed = -0.01
        else:
            left_speed = 0.05
            right_speed = 0.05

        info(f"Left conf: {left_conf:.2f}, Right conf: {right_conf:.2f}, Left speed: {left_speed:.2f}, Right speed: {right_speed:.2f}")
        return ChassisVelocity(
            (left_speed + right_speed) / 2,
            0.0,
            (right_speed - left_speed) / 0.12
        )

    async def _execute_step(self, robot: "GenericRobot") -> None:
        left_conf = 0.0
        right_conf = 0.0

        last_time = asyncio.get_event_loop().time()

        while left_conf <= 0.9 and right_conf <= 0.9:
            current_time = asyncio.get_event_loop().time()
            delta_time = max(current_time - last_time, 0.0)
            last_time = current_time

            left_conf = self.left_sensor.probabilityOfBlack()
            right_conf = self.right_sensor.probabilityOfBlack()

            velocity = self._get_velocity(left_conf, right_conf)

            robot.drive.set_velocity(velocity)
            robot.drive.update(delta_time)

            await asyncio.sleep(0.0)