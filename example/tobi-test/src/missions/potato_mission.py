from libstp import forward_lineup_on_black
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            forward_lineup_on_black(
                left_sensor=Defs.front_left_ir_sensor,
                right_sensor=Defs.front_right_ir_sensor,
            )
        ])
