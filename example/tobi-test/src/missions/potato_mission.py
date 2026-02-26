from libstp import turn_left, auto_tune, drive_forward, turn_right, drive_backward, follow_line
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.logging_step import LoggingStep


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            # LoggingStep(),
            # follow_line(
            #     Defs.front_left_ir_sensor,
            #     Defs.front_right_ir_sensor,
            #     distance_cm=50
            # )
            #auto_tune()
            turn_left(90),
            #turn_right(90),
            #drive_forward(cm=50),
            #drive_backward(cm=50)
            # auto_tune(),
            # tune_drive(),
            # characterize_drive(
            #     axes=["forward", "angular"]
            # )
            # drive_forward(cm=50),
            # turn_left(90)
        ])
