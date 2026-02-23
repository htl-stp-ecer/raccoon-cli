from libstp import drive_forward, auto_tune, turn_right
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            # turn_left(90),
            turn_right(90),
            # drive_forward(cm=50)
            # auto_tune(),
            # tune_drive(),
            # characterize_drive(
            #     axes=["forward", "angular"]
            # )
            # drive_forward(cm=50),
            # turn_left(90)
        ])
