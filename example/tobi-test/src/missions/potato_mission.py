from libstp import Step, GenericRobot, wait_for_button, loop_forever, wait
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.thresholded_sensor.wait_for_threshold import wait_for_threshold
from src.steps.timestamp_test import lineup
from src.steps.drum_collector import drum_advance, drum_retreat


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            loop_forever(seq([
                wait_for_threshold(Defs.drum_distance_sensor),
                wait(1),
                drum_retreat(),
                #wait_for_button(),
            ])),

            # lineup(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            # ),

            # edge_lineup_on_black(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            # )
            # ReverseAlignOnEdge(
            #     left_sensor=Defs.front_left_ir_sensor,
            #     right_sensor=Defs.front_right_ir_sensor,
            # )
            # LineupUHH(left_sensor=Defs.front_left_ir_sensor, right_sensor=Defs.front_right_ir_sensor)
        ])

class PrintSensorDistance(Step):
    async def _execute_step(self, robot: "GenericRobot") -> None:
        self.info(f"Distance between: ${robot.distance_between_sensors(
            Defs.front_left_ir_sensor,
            Defs.front_right_ir_sensor
        )}")