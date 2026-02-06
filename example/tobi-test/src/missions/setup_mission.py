from libstp.mission.api import Mission
from libstp.robot import GenericRobot
from libstp.step.sequential import Sequential, seq
from libstp import wait_for_button, calibrate_distance, Step

from src.hardware.defs import Defs
from src.steps.drum_collector import calibrate_drum_collector


class SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            wait_for_button(),
            calibrate_drum_collector(),
            #calibrate_distance(calibrate_light_sensors=True, distance_cm=10),
            #calibrate_sensors(),
            wait_for_button()
        ])