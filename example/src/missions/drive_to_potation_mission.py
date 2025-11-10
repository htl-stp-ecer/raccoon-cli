from libstp.mission.api import Mission
from libstp.robot.api import GenericRobot
from libstp.step import Step
from libstp.step.drive import drive_forward
from libstp.step.sequential import Sequential, seq



class G(Step):

    async def run_step(self, robot: "Robot") -> None:
        robot.odometry.reset()


class DriveToPotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            G(),
            drive_forward(cm=10),
            # timeout(2),
            # drive_backward(cm=5),
            # parallel(
            # ),
        ])
