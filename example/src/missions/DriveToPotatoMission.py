from libstp_helpers.api.missions import Mission
from libstp_helpers.api.steps.sequential import Sequential, seq
from libstp_helpers.api.steps.motor import motor
from libstp_helpers.api.steps import turn_cw
from libstp_helpers.api.steps.custom_step import custom_step
from libstp_helpers.api.steps.motion.lineup import forward_lineup_on_white
from libstp_helpers.api.steps import drive_forward
from libstp_helpers.api.steps import drive_backward
from libstp_helpers.api.steps.motion.lineup import backward_lineup_on_white
from libstp_helpers.api.steps import parallel
from libstp_helpers.api.steps import turn_ccw

class DriveToPotato(Mission):
    def sequence(self) -> Sequential:
        return seq([
            drive_forward(1.5, 1),
            seq([
                custom_step(lambda _, defs: defs.synchronizer.start_recording()),
                drive_forward(1.5, 1),
                drive_forward(0.2, 1),
                turn_ccw(65, 1),
                backward_lineup_on_white(l_sensor, r_sensor, ki=0.1),
                drive_forward(0.1, 1),
                turn_cw(90, 1),
                parallel(
                    seq([
                        drive_forward(1.5, 1),
                        turn_ccw(15, 1),
                        drive_forward(0.5, 1)
                    ]),
                    turn_ccw(7, 1),
                    motor(Flaschen_motor, "-55", 1)
                ),
                drive_backward(2.5, 1),
                turn_cw(5, 1),
                drive_backward(5, 1, False),
                turn_ccw(18, 1),
                drive_backward(2.2, 1),
                turn_cw(7, 1),
                drive_backward(2.5, 1),
                forward_lineup_on_white(l_sensor, r_sensor, 0.6),
                drive_forward(0.4, 1),
                turn_ccw(90, 1)
            ])
        ])
