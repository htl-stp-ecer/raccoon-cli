import asyncio
from typing import List, Tuple

from libstp import (
    GenericRobot,
    AnalogSensor,
    Motor,
    KMeans,
    RobotService,
    IRSensor
)

NUM_POCKETS = 9
DEFAULT_MOTOR_SPEED = 0.7
SAMPLE_INTERVAL = 0.01  # ~100 Hz
HYSTERESIS_FRACTION = 0.3  # fraction of spread used as dead zone on each side of midpoint
EMA_ALPHA = 0.9  # low-pass filter smoothing factor (lower = smoother, slower response)


class DrumMotorService(RobotService):
    """Business logic for the drum collector: calibration, pocket navigation."""

    def __init__(self, robot: "GenericRobot") -> None:
        super().__init__(robot)
        self._blocked_threshold: float | None = None
        self._pocket_threshold: float | None = None
        self._current_index: int = 0

    @property
    def motor(self) -> Motor:
        return self.robot.defs.drum_motor

    @property
    def light_sensor(self) -> AnalogSensor:
        return self.robot.defs.drum_light_sensor

    # ── calibration ──────────────────────────────────────────────

    @property
    def is_calibrated(self) -> bool:
        return self._blocked_threshold is not None and self._pocket_threshold is not None

    @property
    def midpoint(self) -> float:
        """Midpoint between blocked and pocket thresholds."""
        assert self.is_calibrated, "Drum not calibrated"
        return (self._blocked_threshold + self._pocket_threshold) / 2

    @property
    def hysteresis_thresholds(self) -> Tuple[float, float]:
        """Return (low, high) thresholds with dead zone around midpoint.

        To transition pocket→blocked the reading must exceed *high*.
        To transition blocked→pocket the reading must drop below *low*.
        """
        assert self.is_calibrated, "Drum not calibrated"
        mid = self.midpoint
        band = (self._blocked_threshold - self._pocket_threshold) * HYSTERESIS_FRACTION
        return (mid - band, mid + band)

    @property
    def blocked_threshold(self) -> float | None:
        return self._blocked_threshold

    @property
    def pocket_threshold(self) -> float | None:
        return self._pocket_threshold

    async def sample(self, duration: float, motor_speed: float = DEFAULT_MOTOR_SPEED) -> List[float]:
        """Spin motor and collect light sensor readings at ~100 Hz."""
        self.info(f"Sampling: duration={duration}s, motor_speed={motor_speed}")
        samples: List[float] = []
        speed_percent = int(motor_speed * 100)
        self.info(f"Setting motor speed to {speed_percent}%")
        self.motor.set_speed(speed_percent)
        try:
            loop = asyncio.get_event_loop()
            t_end = loop.time() + duration
            while loop.time() < t_end:
                val = float(self.light_sensor.read())
                samples.append(val)
                await asyncio.sleep(SAMPLE_INTERVAL)
        finally:
            self.motor.set_speed(0)
            self.motor.brake()
        if samples:
            self.info(f"Collected {len(samples)} samples in {duration}s — min={min(samples):.0f}, max={max(samples):.0f}, avg={sum(samples)/len(samples):.0f}")
        else:
            self.info(f"Collected 0 samples in {duration}s")
        return samples

    def cluster(self, samples: List[float]) -> Tuple[float, float]:
        """Run 2-means clustering. Returns (pocket, blocked) centroids."""
        self.info(f"Clustering {len(samples)} samples...")
        km = KMeans(max_iterations=10)
        result = km.fit(samples)
        pocket = min(result.centroid1, result.centroid2)
        blocked = max(result.centroid1, result.centroid2)
        spread = blocked - pocket
        self.info(f"Cluster result: pocket={pocket:.0f}, blocked={blocked:.0f}, spread={spread:.0f}")
        return pocket, blocked

    def apply_calibration(self, blocked: float, pocket: float) -> None:
        """Store thresholds and calibrate the IR sensor if applicable."""
        self._blocked_threshold = blocked
        self._pocket_threshold = pocket
        if isinstance(self.light_sensor, IRSensor):
            self.light_sensor.setCalibration(blocked, pocket)
        low, high = self.hysteresis_thresholds
        self.info(
            f"Calibration applied: pocket={pocket:.0f}, blocked={blocked:.0f}, "
            f"spread={blocked - pocket:.0f}, hysteresis=[{low:.0f}, {high:.0f}]"
        )

    # ── pocket navigation ────────────────────────────────────────

    @property
    def current_index(self) -> int:
        return self._current_index

    async def advance(self, count: int = 1) -> None:
        """Spin forward through *count* pocket edges."""
        self.info(f"advance({count}) from index {self._current_index}")
        assert self.is_calibrated, "Drum not calibrated"
        await self._move(count, forward=True)

    async def retreat(self, count: int = 1) -> None:
        """Spin backward through *count* pocket edges."""
        self.info(f"retreat({count}) from index {self._current_index}")
        assert self.is_calibrated, "Drum not calibrated"
        await self._move(count, forward=False)

    async def go_to(self, index: int) -> None:
        """Move to target pocket index via the shortest path (wrapping)."""
        self.info(f"go_to({index}) from current index {self._current_index}")
        assert self.is_calibrated, "Drum not calibrated"
        delta = (index - self._current_index) % NUM_POCKETS
        if delta == 0:
            self.info(f"Already at index {index}, no move needed")
            return
        # shortest direction
        if delta <= NUM_POCKETS // 2:
            self.info(f"Shortest path: advance {delta}")
            await self.advance(delta)
        else:
            self.info(f"Shortest path: retreat {NUM_POCKETS - delta}")
            await self.retreat(NUM_POCKETS - delta)

    async def add_offset(self, offset_ticks: int, move_speed: int = 0.3, tolerance: float = 5.0) -> None:
        """Move motor by ticks"""
        current_position = self.motor.get_position()
        target_position = current_position + offset_ticks
        self.info(f"add_offset({offset_ticks} ticks): current={current_position}, target={target_position}")
        while True:
            current_position = self.motor.get_position()
            error = target_position - current_position
            if abs(error) < tolerance:  # tolerance in ticks
                self.info(f"Reached target position within tolerance: current={current_position}, target={target_position}")
                break
            speed_percent = int(move_speed * 100) * (1 if error > 0 else -1)
            self.motor.set_speed(speed_percent)
            await asyncio.sleep(SAMPLE_INTERVAL)
        self.motor.set_speed(0)
        self.motor.brake()

    async def _move(self, count: int, *, forward: bool) -> None:
        """Low-level: spin motor and count edge transitions on the sensor.

        Uses an exponential moving average (EMA) to filter sensor noise, plus
        hysteresis thresholds to avoid false triggers near the midpoint.
        """
        low, high = self.hysteresis_thresholds
        direction = "forward" if forward else "backward"
        speed_percent = int(DEFAULT_MOTOR_SPEED * 100) * (1 if forward else -1)
        self.info(f"Moving {direction} {count} pockets (hyst=[{low:.0f},{high:.0f}], speed={speed_percent}%)")
        self.motor.set_speed(speed_percent)
        try:
            pockets = 0
            # Seed the EMA with a few readings so it starts stable
            raw = float(self.light_sensor.read())
            filtered = raw
            for _ in range(5):
                await asyncio.sleep(SAMPLE_INTERVAL)
                raw = float(self.light_sensor.read())
                filtered += EMA_ALPHA * (raw - filtered)

            # Determine initial state
            if filtered >= high:
                is_blocked = True
            elif filtered <= low:
                is_blocked = False
            else:
                is_blocked = filtered > self.midpoint
            self.info(f"Initial raw={raw:.0f}, filtered={filtered:.0f}, state={'blocked' if is_blocked else 'pocket'}")

            # Each pocket = 2 edges (blocked→pocket + pocket→blocked).
            # We count completed pockets, incrementing index after every 2nd edge.
            half = 0  # counts edges within the current pocket
            while pockets < count:
                raw = float(self.light_sensor.read())
                filtered += EMA_ALPHA * (raw - filtered)
                if is_blocked and filtered <= low:
                    is_blocked = False
                    half += 1
                    self.info(f"Pocket {pockets}/{count} edge {half}/2: raw={raw:.0f}, filtered={filtered:.0f} → pocket")
                elif not is_blocked and filtered >= high:
                    is_blocked = True
                    half += 1
                    self.info(f"Pocket {pockets}/{count} edge {half}/2: raw={raw:.0f}, filtered={filtered:.0f} → blocked")
                if half >= 2:
                    half = 0
                    pockets += 1
                    if forward:
                        self._current_index = (self._current_index + 1) % NUM_POCKETS
                    else:
                        self._current_index = (self._current_index - 1) % NUM_POCKETS
                    self.info(f"Pocket {pockets}/{count} complete → index={self._current_index}")
                await asyncio.sleep(SAMPLE_INTERVAL)
        finally:
            self.motor.set_speed(0)
            self.motor.brake()
        self.info(f"Move complete: {direction} {count} pockets → index {self._current_index}")
