from libstp.mission.api import Mission
from libstp.robot.api import GenericRobot
from libstp.step import Step
from libstp.step.servo import servo
from libstp.step.wait_for_seconds import wait
from libstp.step.drive import drive_forward, drive_backward
from libstp.step.turn import turn_cw, turn_ccw
from libstp.step.sequential import Sequential, seq
from libstp.step.strafe import strafe_left

from src.hardware.defs import Defs


class G(Step):

    async def run_step(self, robot: "Robot") -> None:
        robot.odometry.reset()


class DriveToPotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            servo(Defs.potato_server, 90)
            # turn_cw(90, 1),
            # wait(5),
            # turn_ccw(90, 1),
            # drive_forward(cm=25),
            # strafe_left(20, 1),
            # #wait(2),
            # turn_ccw(90, 1)
            # # parallel(
            # # ),
        ])
