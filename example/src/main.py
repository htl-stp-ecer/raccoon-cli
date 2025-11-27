from libstp.calibration import CalibrationConfig

from src.hardware.robot import Robot

robot = Robot()

# # Default calibration
# results = robot.kinematics.calibrate_motors()
# #
# # # Custom config (works for both differential and mecanum)
# # config = CalibrationConfig()
# # config.use_relay_feedback = True  # Aggressive mode
# # results = robot.drive.calibrate_motors(config)
#
# # Check results
# for i, result in enumerate(results):
#   if result.success:
#       print(f"Motor {i}: PID: kp={result.pid.kp}, ki={result.pid.ki}, kd={result.pid.kd}, FF: kS={result.ff.kS}, kV={result.ff.kV}, kA={result.ff.kA}")
#

if __name__ == "__main__":
    robot.start()
