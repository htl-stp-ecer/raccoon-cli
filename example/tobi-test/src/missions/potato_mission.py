from libstp import Step, GenericRobot, wait_for_button, wait_for_checkpoint
from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq

from src.hardware.defs import Defs
from src.steps.drum_collector import drum_retreat
from src.steps.drum_pusher_servo import open_drum_pusher, close_drum_pusher


class PotatoMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            # TuneTurn(angle=90),
            # turn_left(90),
            # loop_forever(seq([
            #     open_drum_pusher(),
            #     #move_drum_motor_by_offset(-250),
            #     wait_for_button(),
            #     #wait_for_threshold(Defs.drum_distance_sensor),
            #     #wait(0.25),
            #     close_drum_pusher(),
            #     drum_retreat(),
            #     #wait_for_button(),
            # ])),

            open_drum_pusher(),
            wait_for_checkpoint(11),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 8),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 7 + 8),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 7 + 7 + 8),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 7 + 7 + 7 + 8),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 7 + 7 + 7 + 7 + 8),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 7 + 7 + 7 + 7 + 7 + 8),
            close_drum_pusher(),
            drum_retreat(),

            open_drum_pusher(),
            wait_for_checkpoint(10 + 7 + 7 + 7 + 7 + 7 + 7 + 8),
            close_drum_pusher(),
            drum_retreat(),

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
