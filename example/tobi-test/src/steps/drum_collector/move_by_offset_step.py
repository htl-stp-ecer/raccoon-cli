from libstp import Step, GenericRobot, dsl

from src.service.drum_motor_service import DrumMotorService

@dsl(hidden=True)
class MoveDrumMotorByOffsetStep(Step):
    def __init__(self, offset: int):
        super().__init__()
        self.offset = offset

    async def _execute_step(self, robot: "GenericRobot") -> None:
        service = robot.get_service(DrumMotorService)
        await service.add_offset(self.offset)

@dsl
def move_drum_motor_by_offset(offset: int) -> MoveDrumMotorByOffsetStep:
    """Move the drum motor by a relative offset (positive or negative)."""
    return MoveDrumMotorByOffsetStep(offset=offset)