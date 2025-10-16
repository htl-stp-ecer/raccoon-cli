import time

from libstp.drive import ChassisVel
from libstp.hal import DigitalSensor
from libstp.foundation import debug, info, warn, error

from src.hardware.robot import Robot

robot = Robot()
if __name__ == "__main__":
    # robot.defs.front_left_motor.set_speed(100)
    # robot.defs.rear_left_motor.set_speed(100)
    # robot.defs.front_right_motor.set_speed(100)
    # robot.defs.rear_right_motor.set_speed(100)
    dig = DigitalSensor(10)
    # while not dig.read():
    #     time.sleep(0.1)
    cmd = ChassisVel()
    cmd.vx = 0.0
    cmd.vy = 1.0
    cmd.w = 0.0
    robot.drive.set_velocity(cmd)

    while not dig.read():
        robot.drive.update(0.1)
        state = robot.drive.estimate_state()
        debug(f"Vel: {state.vx}, {state.vy}, {state.wz}")
        time.sleep(0.1)
    #robot.start()
