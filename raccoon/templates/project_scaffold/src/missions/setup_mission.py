from libstp import *

from src.hardware.defs import Defs


class SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            calibrate(distance_cm=50),
            wait_for_button(),
        ])
