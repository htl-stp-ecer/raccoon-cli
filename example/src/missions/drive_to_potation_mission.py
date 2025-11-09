from libstp.mission.api import Mission
from libstp.step.drive import drive_forward, drive_backward
from libstp.step.parallel import parallel
from libstp.step.sequential import Sequential, seq
from libstp.step.timeout import timeout


class DriveToPotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            drive_forward(cm=5),
            #timeout(2),
            drive_backward(cm=5),
            #parallel(
            #),
        ])
