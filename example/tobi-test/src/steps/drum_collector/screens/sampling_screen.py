from libstp.ui import *


class DrumSamplingScreen(UIScreen[None]):
    """Screen shown while the drum motor spins and light sensor samples are collected."""

    title = "Drum Calibration"

    def __init__(self, sensor_port: int):
        super().__init__()
        self.sensor_port = sensor_port
        self.sample_count = 0

    def build(self) -> Widget:
        return Split(
            left=[
                Row(children=[
                    ProgressSpinner(size=24),
                    Spacer(8),
                    Text("Sampling...", size="large"),
                ], align="center"),
                Spacer(8),
                Text("Motor is spinning. Collecting light sensor data.", size="small", muted=True),
                Spacer(16),
                Text(f"{self.sample_count} samples", size="small", muted=True),
            ],
            right=[
                Card(title=f"Sensor Port {self.sensor_port}", children=[
                    SensorValue(port=self.sensor_port, sensor_type="analog"),
                ]),
            ],
            ratio=(1, 1),
        )
