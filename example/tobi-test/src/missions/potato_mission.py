from libstp import drive_until_black
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.lineup_uhh import ReverseAlignOnEdge


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            drive_until_black(
                [Defs.front_left_ir_sensor, Defs.front_right_ir_sensor],
                1.0,
            )
            # ReverseAlignOnEdge(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            # )
            # LineupUHH(left_sensor=Defs.front_left_ir_sensor, right_sensor=Defs.front_right_ir_sensor)
        ])
