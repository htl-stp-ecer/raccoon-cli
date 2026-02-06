from libstp.mission.api import Mission
from libstp.robot import GenericRobot
from libstp.step.sequential import Sequential, seq
from libstp import wait_for_button, calibrate_distance, Step

from src.hardware.defs import Defs
from src.steps.drum_collector import calibrate_drum_collector
from src.steps.thresholded_sensor.calibrate_thresholded_sensor import calibrate_threshold_sensor


class SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            wait_for_button(),
            calibrate_drum_collector(),
            calibrate_threshold_sensor(Defs.drum_distance_sensor),
            #calibrate_distance(calibrate_light_sensors=True, distance_cm=10),
            #calibrate_sensors(),
            wait_for_button()
        ])