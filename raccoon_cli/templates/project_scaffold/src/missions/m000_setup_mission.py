from libstp import *

from src.hardware.defs import Defs


class M000SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            calibrate(distance_cm=50),
        ])
