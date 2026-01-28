from libstp.screen.api import RenderScreen
from libstp.sensor_ir import IRSensor
from libstp.robot.api import GenericRobot
from libstp.step import Step

class APIStep(Step):
    async def _execute_step(self) -> None:
        screen = RenderScreen([IRSensor(0)])
        await screen.calibrate_black_white()

def calibrate_sensors_step() -> APIStep:
    return APIStep()
