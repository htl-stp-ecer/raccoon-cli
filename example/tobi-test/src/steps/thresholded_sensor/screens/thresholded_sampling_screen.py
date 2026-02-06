from libstp import UIScreen, Widget, Split, Row, ProgressSpinner, Spacer, Text, Card, SensorValue


class ThresholdedSamplingScreen(UIScreen[None]):
    """Shown during calibration sampling."""

    title = "Spike Sensor Calibration"

    def __init__(self, sensor_port: int):
        super().__init__()
        self.sensor_port = sensor_port

    def build(self) -> Widget:
        return Split(
            left=[
                Row(
                    children=[
                        ProgressSpinner(size=24),
                        Spacer(8),
                        Text("Sampling...", size="large"),
                    ],
                    align="center",
                ),
                Spacer(8),
                Text(
                    "Roll the object underneath the sensor now.",
                    size="small",
                    muted=True,
                ),
            ],
            right=[
                Card(
                    title=f"Sensor Port {self.sensor_port}",
                    children=[
                        SensorValue(port=self.sensor_port, sensor_type="analog"),
                    ],
                ),
            ],
            ratio=(1, 1),
        )
