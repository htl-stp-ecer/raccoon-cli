from libstp import drive_until_black, forward_lineup_on_black
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.lineup import edge_lineup_on_black


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            edge_lineup_on_black(
                left_sensor=Defs.front_left_ir_sensor,
                right_sensor=Defs.front_right_ir_sensor,
            )
            # ReverseAlignOnEdge(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            # )
            # LineupUHH(left_sensor=Defs.front_left_ir_sensor, right_sensor=Defs.front_right_ir_sensor)
        ])
