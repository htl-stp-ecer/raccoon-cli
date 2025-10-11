import time

from libstp.drive import ChassisVel
from libstp.hal import DigitalSensor

from src.hardware.robot import Robot

robot = Robot()
if __name__ == "__main__":
    robot.defs.front_left_motor.set_speed(100)
    robot.defs.rear_left_motor.set_speed(100)
    robot.defs.front_right_motor.set_speed(100)
    robot.defs.rear_right_motor.set_speed(100)
    dig = DigitalSensor(10)
    while not dig.read():
        time.sleep(0.1)
    # cmd = ChassisVel()
    # cmd.vx = -1.0
    # cmd.vy = 0.0
    # cmd.w = 0.0
    # robot.drive.set_velocity(cmd)
    # while True:
    #     robot.drive.update(0.1)
    #     state = robot.drive.estimate_state()
    #     print(f"vx: {state.vx:.2f}, vy: {state.vy:.2f}, wz: {state.wz:.2f}")
    #     time.sleep(0.1)
    #robot.start()
