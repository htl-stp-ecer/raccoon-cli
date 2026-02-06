from typing import List

from libstp.ui import *

from ..dataclasses import DrumCalibrationResult


class DrumConfirmScreen(UIScreen[DrumCalibrationResult]):
    """Confirm drum calibration thresholds (blocked vs pocket)."""

    title = "Drum Calibration"

    def __init__(
        self,
        blocked_threshold: float,
        pocket_threshold: float,
        collected_values: List[float],
    ):
        super().__init__()
        self.blocked_threshold = blocked_threshold
        self.pocket_threshold = pocket_threshold
        self.collected_values = collected_values

    @property
    def is_good(self) -> bool:
        return abs(self.blocked_threshold - self.pocket_threshold) > 100

    def build(self) -> Widget:
        return Split(
            left=[
                Row(children=[
                    StatusIcon(
                        icon="check" if self.is_good else "warning",
                        color="green" if self.is_good else "orange",
                    ),
                    Spacer(8),
                    Text(
                        "Calibration Complete" if self.is_good else "Low Contrast",
                        size="large",
                    ),
                ], align="center"),
                Spacer(12),
                Row(children=[
                    Column(children=[
                        Text("Blocked", size="small", muted=True),
                        NumericInput(id="blocked", value=self.blocked_threshold),
                    ], spacing=2),
                    Column(children=[
                        Text("Pocket", size="small", muted=True),
                        NumericInput(id="pocket", value=self.pocket_threshold),
                    ], spacing=2),
                ], spacing=16),
            ],
            right=[
                ResultsTable(rows=[
                    ("Blocked (dark)", f"{self.blocked_threshold:.0f}", "grey"),
                    ("Pocket (light)", f"{self.pocket_threshold:.0f}", "white"),
                    ("Diff", f"{abs(self.blocked_threshold - self.pocket_threshold):.0f}",
                     "green" if self.is_good else "orange"),
                    ("Samples", f"{len(self.collected_values)}", "blue"),
                ]),
                Spacer(12),
                Row(children=[
                    Button("retry", "Retry", style="secondary"),
                    Button("confirm", "Confirm",
                           style="success" if self.is_good else "warning"),
                ], spacing=8),
            ],
            ratio=(1, 1),
        )

    @on_change("blocked")
    async def on_blocked_change(self, value: float):
        self.blocked_threshold = value
        await self.refresh()

    @on_change("pocket")
    async def on_pocket_change(self, value: float):
        self.pocket_threshold = value
        await self.refresh()

    @on_click("retry")
    async def on_retry(self):
        self.close(DrumCalibrationResult(
            confirmed=False,
            blocked_threshold=self.blocked_threshold,
            pocket_threshold=self.pocket_threshold,
        ))

    @on_click("confirm")
    async def on_confirm(self):
        self.close(DrumCalibrationResult(
            confirmed=True,
            blocked_threshold=self.blocked_threshold,
            pocket_threshold=self.pocket_threshold,
        ))
