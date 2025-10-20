import math
import time

from libstp.hal import IMU

from src.hardware.robot import Robot


def quaternion_to_euler(w, x, y, z):
    # Roll (x-axis rotation)
    t0 = 2.0 * (w * x + y * z)
    t1 = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(t0, t1)

    # Pitch (y-axis rotation)
    t2 = 2.0 * (w * y - z * x)
    t2 = max(-1.0, min(1.0, t2))  # clamp
    pitch = math.asin(t2)

    # Yaw (z-axis rotation)
    t3 = 2.0 * (w * z + x * y)
    t4 = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(t3, t4)

    return (
        math.degrees(roll),
        math.degrees(pitch),
        math.degrees(yaw)
    )


robot = Robot()
if __name__ == "__main__":
    imu = IMU()
    while True:
        w, x, y, z = imu.get_orientation()
        print(quaternion_to_euler(w, x, y, z))
        time.sleep(0.5)
    motion = DriveStraightMotion(robot.drive, imu, 10, 1)
    # robot.defs.front_left_motor.set_speed(100)
    # robot.defs.rear_left_motor.set_speed(100)
    # robot.defs.front_right_motor.set_speed(100)
    # robot.defs.rear_right_motor.set_speed(100)
    # dig = DigitalSensor(10)
    # # while not dig.read():
    # #     time.sleep(0.1)
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
    # robot.start()
