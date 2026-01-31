from libstp import forward_lineup_on_black, turn_left
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.timestamp_test import TimestampTest, timestamp_test


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            turn_left(90)
            # timestamp_test(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            #     sensor_distance_m=90),
            # forward_lineup_on_black(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor
            # )
            # TimestampTest(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            #     forward_speed=1.0
            # )
        ])
