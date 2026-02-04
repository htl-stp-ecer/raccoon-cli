from libstp.mission.api import Mission
from libstp.step.sequential import Sequential, seq
from libstp import wait_for_button, calibrate_distance


class SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            calibrate_distance(calibrate_light_sensors=True),
            #calibrate_sensors(),
            wait_for_button()
        ])
