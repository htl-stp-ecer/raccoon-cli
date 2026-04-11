from raccoon import *

from src.hardware.defs import Defs


class M000SetupMission(SetupMission):
    setup_time = 120

    def sequence(self) -> Sequential:
        return seq([
            calibrate(distance_cm=50),
        ])
