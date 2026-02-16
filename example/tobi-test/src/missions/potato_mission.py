from libstp import drive_forward, turn_left
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            # characterize_drive(
            #     axes=["forward", "angular"]
            # )
            drive_forward(cm=50),
            turn_left(90)
        ])
