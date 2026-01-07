from libstp.hal import Servo


class CustomServo(Servo):
    def __init__(self, port):
        super().__init__(port)