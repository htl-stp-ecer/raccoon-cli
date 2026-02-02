from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            LineupUHH(left_sensor=Defs.front_left_ir_sensor, right_sensor=Defs.front_right_ir_sensor)
        ])
