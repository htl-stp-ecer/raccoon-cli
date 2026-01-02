from libstp import calibration
from libstp.button import set_digital

from src.hardware.robot import Robot
from src.missions.drive_to_potato_mission import DriveToPotatoMission

robot = Robot()
robot.missions = [
    DriveToPotatoMission(),
]
set_digital(10)

# cfg = calibration.MotionCalibrationConfig()
# cfg.control_rate_hz = 100.0          # Fixed loop rate for the test
# cfg.relay_yaw_rate = 0.4             # rad/s relay amplitude (±)
# cfg.forward_speed_mps = 0.25         # drive speed during tuning
# cfg.max_heading_autotune_time = 20.0 # safety timeout
# cfg.min_heading_cycles = 5
# cfg.settle_skip_cycles = 1
# cfg.min_peak_separation = 0.15
# cfg.min_heading_amplitude = 0.01
#
# # Run calibration (currently only drive-straight heading autotune is implemented)
# cal = calibration.MotionCalibrator(robot.drive, robot.odometry, cfg)
# result = cal.calibrate_drive_straight_motion()
#
# if result.success:
#   heading_gains = [g for g in result.gains if "heading" in g.controller_name]
#   g = heading_gains[0]
#   print(f"Heading PID: Kp={g.kp:.4f}, Ki={g.ki:.4f}, Kd={g.kd:.4f}")
# else:
#   print("Autotune failed:", result.error_message)

if __name__ == "__main__":
   robot.start()
