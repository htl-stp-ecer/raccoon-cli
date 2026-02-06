from typing import List

from libstp import UIScreen, Widget, Split, Row, StatusIcon, Spacer, Text, Column, NumericInput, ResultsTable, Button, \
    on_change, on_click

from src.steps.thresholded_sensor.dataclasses import ThresholdedCalibrationResult


class ThresholdedConfirmScreen(UIScreen[ThresholdedCalibrationResult]):
    """Confirm spike calibration results."""

    title = "Spike Sensor Calibration"

    def __init__(
            self,
            baseline: float,
            spike_amplitude: float,
            threshold: float,
            samples: List[float],
    ):
        super().__init__()
        self.baseline = baseline
        self.spike_amplitude = spike_amplitude
        self.threshold = threshold
        self.samples = samples

    @property
    def is_good(self) -> bool:
        return self.spike_amplitude > 20

    def build(self) -> Widget:
        return Split(
            left=[
                Row(
                    children=[
                        StatusIcon(
                            icon="check" if self.is_good else "warning",
                            color="green" if self.is_good else "orange",
                        ),
                        Spacer(8),
                        Text(
                            "Spike Detected" if self.is_good else "Weak Spike",
                            size="large",
                        ),
                    ],
                    align="center",
                ),
                Spacer(12),
                Row(
                    children=[
                        Column(
                            children=[
                                Text("Baseline", size="small", muted=True),
                                NumericInput(id="baseline", value=self.baseline),
                            ],
                            spacing=2,
                        ),
                        Column(
                            children=[
                                Text("Threshold", size="small", muted=True),
                                NumericInput(id="threshold", value=self.threshold),
                            ],
                            spacing=2,
                        ),
                    ],
                    spacing=16,
                ),
            ],
            right=[
                ResultsTable(
                    rows=[
                        ("Baseline", f"{self.baseline:.0f}", "white"),
                        ("Spike Amplitude", f"{self.spike_amplitude:.0f}", "blue"),
                        (
                            "Threshold",
                            f"{self.threshold:.0f}",
                            "green" if self.is_good else "orange",
                        ),
                        ("Samples", f"{len(self.samples)}", "grey"),
                    ]
                ),
                Spacer(12),
                Row(
                    children=[
                        Button("retry", "Retry", style="secondary"),
                        Button(
                            "confirm",
                            "Confirm",
                            style="success" if self.is_good else "warning",
                        ),
                    ],
                    spacing=8,
                ),
            ],
            ratio=(1, 1),
        )

    @on_change("baseline")
    async def on_baseline_change(self, value: float):
        self.baseline = value
        await self.refresh()

    @on_change("threshold")
    async def on_threshold_change(self, value: float):
        self.threshold = value
        await self.refresh()

    @on_click("retry")
    async def on_retry(self):
        self.close(
            ThresholdedCalibrationResult(
                confirmed=False,
                baseline=self.baseline,
                threshold=self.threshold,
            )
        )

    @on_click("confirm")
    async def on_confirm(self):
        self.close(
            ThresholdedCalibrationResult(
                confirmed=True,
                baseline=self.baseline,
                threshold=self.threshold,
            )
        )
