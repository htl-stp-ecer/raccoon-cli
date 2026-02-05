from libstp.mission.api import Mission
from libstp.robot import GenericRobot
from libstp.step.sequential import Sequential, seq
from libstp import wait_for_button, calibrate_distance, Step

from src.hardware.defs import Defs



class SetupMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
            calibrate_distance(calibrate_light_sensors=True, distance_cm=50),
            #calibrate_sensors(),
            wait_for_button()
        ])