from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq
from libstp.step.calibration.calibrate import calibrate_sensors
from libstp.step.wait_for_button import wait_for_button

class SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            calibrate_sensors(),
            wait_for_button()
        ])
