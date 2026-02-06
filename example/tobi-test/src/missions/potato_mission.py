from libstp import Step, GenericRobot, wait_for_button, loop_forever
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.timestamp_test import lineup
from src.steps.drum_collector import drum_advance


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            loop_forever(seq([
                drum_advance(),
                wait_for_button(),
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