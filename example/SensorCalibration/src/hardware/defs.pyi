"""Auto-generated type stub for defs.py — DO NOT EDIT."""

from libstp import AnalogSensor
from libstp import DigitalSensor
from libstp import IMU as Imu
from libstp import IRSensor
from libstp import Motor
from libstp.step import Step
from libstp.step.motion.sensor_group import SensorGroup
from libstp.step.servo.preset import ServoPreset
from typing import List


class _ShildPreset(ServoPreset):
    def up(self, speed: float | None = None) -> Step: ...
    def down(self, speed: float | None = None) -> Step: ...
    @property
    def device(self) -> "Servo": ...


class _PomArmPreset(ServoPreset):
    def down(self, speed: float | None = None) -> Step: ...
    def above_pom(self, speed: float | None = None) -> Step: ...
    def up(self, speed: float | None = None) -> Step: ...
    def start(self, speed: float | None = None) -> Step: ...
    def high_up(self, speed: float | None = None) -> Step: ...
    @property
    def device(self) -> "Servo": ...


class _ShildGraberPreset(ServoPreset):
    def open(self, speed: float | None = None) -> Step: ...
    def closed(self, speed: float | None = None) -> Step: ...
    @property
    def device(self) -> "Servo": ...


class _PomGrabPreset(ServoPreset):
    def closed(self, speed: float | None = None) -> Step: ...
    def start(self, speed: float | None = None) -> Step: ...
    def pom_width(self, speed: float | None = None) -> Step: ...
    def slightly_open(self, speed: float | None = None) -> Step: ...
    def open(self, speed: float | None = None) -> Step: ...
    def wide_open(self, speed: float | None = None) -> Step: ...
    @property
    def device(self) -> "Servo": ...


class Defs:
    imu: Imu
    button: DigitalSensor
    rear_right_light_sensor: IRSensor
    wait_for_light_sensor: AnalogSensor
    front_right_light_sensor: IRSensor
    front_left_light_sensor: IRSensor
    front: SensorGroup
    rear: SensorGroup
    front_left_motor: Motor
    front_right_motor: Motor
    rear_left_motor: Motor
    rear_right_motor: Motor
    shild: _ShildPreset
    pom_arm: _PomArmPreset
    shild_graber: _ShildGraberPreset
    pom_grab: _PomGrabPreset
    analog_sensors: List[AnalogSensor]
