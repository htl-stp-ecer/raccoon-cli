from libstp import servo

from src.hardware.defs import Defs


def open_drum_pusher():
    return servo(Defs.drum_pusher_servo, 170)

def close_drum_pusher():
    return servo(Defs.drum_pusher_servo, 0)