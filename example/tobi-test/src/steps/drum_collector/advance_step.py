from libstp import GenericRobot, dsl
from libstp.step import Step

from src.service.drum_motor_service import DrumMotorService


@dsl(hidden=True)
class DrumAdvanceStep(Step):
    def __init__(self, count: int = 1):
        super().__init__()
        self.count = count

    async def _execute_step(self, robot: "GenericRobot") -> None:
        service = robot.get_service(DrumMotorService)
        await service.advance(self.count)


@dsl()
def drum_advance(count: int = 1) -> DrumAdvanceStep:
    """Advance the drum forward by count pockets."""
    return DrumAdvanceStep(count=count)
