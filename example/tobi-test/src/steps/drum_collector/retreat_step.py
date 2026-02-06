from libstp import GenericRobot, dsl
from libstp.step import Step

from src.service.drum_motor_service import DrumMotorService


@dsl(hidden=True)
class DrumRetreatStep(Step):
    def __init__(self, count: int = 1):
        super().__init__()
        self.count = count

    async def _execute_step(self, robot: "GenericRobot") -> None:
        service = robot.get_service(DrumMotorService)
        await service.retreat(self.count)


@dsl()
def drum_retreat(count: int = 1) -> DrumRetreatStep:
    """Retreat the drum backward by count pockets."""
    return DrumRetreatStep(count=count)
