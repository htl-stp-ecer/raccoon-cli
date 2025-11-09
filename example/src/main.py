import math
import time

from libstp.hal import IMU, DigitalSensor
from libstp.motion import DriveStraightMotion, DriveStraightConfig, TurnMotionConfig, TurnMotion
from libstp.odometry_imu import ImuOdometry

from src.hardware.robot import Robot


robot = Robot()
if __name__ == "__main__":
    # dig = robot.defs.button
    # # config = DriveStraightConfig()
    # # config.distance_m = 0.1
    # # config.distance_tolerance_m = 0.01
    # # config.heading_kp = 50
    # # config.max_speed_mps = 1.0
    # # motion = DriveStraightMotion(robot.drive, robot.odometry, config)
    # config = TurnMotionConfig()
    # config.angle_deg = 180.0
    # config.max_angular_speed_rps = 2.0
    # config.angle_tolerance_deg = 1.0
    # config.angle_kp = 3.5
    # motion = TurnMotion(robot.drive, robot.odometry, config)
    #
    # while not dig.read():
    #     time.sleep(0.1)
    #
    # while dig.read():
    #     time.sleep(0.1)
    #
    # #
    # while not motion.is_finished():
    #     motion.update(0.1)
    #     time.sleep(0.1)

    # robot.defs.front_left_motor.set_speed(10)
    # robot.defs.rear_left_motor.set_speed(10)
    # robot.defs.front_right_motor.set_speed(10)
    # robot.defs.rear_right_motor.set_speed(10)
    # while not dig.read():
    #     time.sleep(0.1)
    # cmd = ChassisVel()
    # cmd.vx = 0.0
    # cmd.vy = 1.0
    # cmd.w = 0.0
    # robot.drive.set_velocity(cmd)
    #
    # while not dig.read():
    #     robot.drive.update(0.1)
    #     state = robot.drive.estimate_state()
    #     debug(f"Vel: {state.vx}, {state.vy}, {state.wz}")
    #     time.sleep(0.1)
    robot.start()
