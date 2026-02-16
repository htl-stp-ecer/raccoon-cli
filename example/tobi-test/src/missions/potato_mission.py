from libstp import drive_forward, turn_left, tune_drive, auto_tune
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            auto_tune(),
            #tune_drive(),
            # characterize_drive(
            #     axes=["forward", "angular"]
            # )
            #drive_forward(cm=50),
            #turn_left(90)
        ])
