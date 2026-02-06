from libstp import GenericRobot, dsl
from libstp.step import Step

from src.service.drum_motor_service import DrumMotorService


@dsl(hidden=True)
class DrumGoToStep(Step):
    def __init__(self, index: int):
        super().__init__()
        self.index = index

    async def _execute_step(self, robot: "GenericRobot") -> None:
        service = robot.get_service(DrumMotorService)
        await service.go_to(self.index)


@dsl()
def drum_go_to(index: int) -> DrumGoToStep:
    """Move the drum to a specific pocket index via the shortest path."""
    return DrumGoToStep(index=index)
