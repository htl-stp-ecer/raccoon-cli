from libstp import forward_lineup_on_black, drive_until_black, timestamp_lineup
from libstp.mission.api import Mission
from libstp.step.motion.simple_lineup import SimpleLineUp
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            SimpleLineUp(
                left_sensor=Defs.front_left_ir_sensor,
                right_sensor=Defs.front_right_ir_sensor
            )
        ])
