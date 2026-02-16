from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            drive_forward(cm=50),
            turn_cw(deg=90),
            drive_forward(cm=93),
            turn_cw(deg=90),
            drive_forward(cm=45),
            turn_cw(deg=90),
            drive_forward(cm=66)
        ])
