"""Resolve the *current mission and step* from a stream of log records.

The library logs a run's structure as it executes: it preloads a known set of
setup / main / shutdown missions, announces ``Running <phase> mission`` and
``Starting mission: <Name>`` / ``Completed mission: <Name>`` as it walks them,
and prints a per-step marker like ``4/20: StartCameraStep`` (or nested
``10/20 > 3/4: CollectDrive(...)``) as each step runs.

:class:`RunProgress` folds that stream into a small live state — which phase,
which mission (and its position in the run), and which step — so a viewer can
show a breadcrumb like ``main · M020CollectDrumsMission (2/5) · step 10/20 ›
3/4 CollectDrive`` instead of leaving the user to eyeball raw log lines.

It is pure and duck-typed: :meth:`RunProgress.update` accepts anything with
``.message`` and ``.func`` string attributes (both
:class:`~raccoon_cli.logs.parser.LogEntry` and
:class:`~raccoon_cli.logs.live_stream.LiveRecord` qualify), so the live
``raccoon run`` TUI and the post-hoc ``raccoon logs`` viewer share one resolver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

# Mission lifecycle markers emitted by raccoon.robot.api.
_PRELOAD_SETUP = re.compile(r"Preloading setup mission:\s*(\S+)")
_PRELOAD_MAIN = re.compile(r"Preloading main mission:\s*(\S+)")
_PRELOAD_SHUTDOWN = re.compile(r"Preloading shutdown mission:\s*(\S+)")
_RUNNING_PHASE = re.compile(r"Running (setup|main|shutdown) mission")
_START_MISSION = re.compile(r"Starting mission:\s*(\S+)")
_COMPLETE_MISSION = re.compile(r"Completed mission:\s*(\S+)")

# Per-step progress marker: "4/20: StartCameraStep" or, when a step nests its own
# sub-sequence, "10/20 > 3/4: CollectDrive(...)". We keep the numeric position
# and the leading identifier of the step name (dropping any "(args)" tail).
_STEP_MARKER = re.compile(
    r"^\s*(\d+)\s*/\s*(\d+)"  # outer  n/total
    r"(?:\s*>\s*(\d+)\s*/\s*(\d+))?"  # optional inner  n/total
    r"\s*:\s*([A-Za-z_]\w*)"  # step name identifier
)


@dataclass
class RunProgress:
    """Live-resolved position within a run: phase, mission, step.

    Feed records in order with :meth:`update`; read the fields (or
    :meth:`breadcrumb`) for the current state. Robust to the library's
    per-phase ``elapsed`` reset — resolution is driven purely by message text,
    never by timing.
    """

    setup_mission: Optional[str] = None
    shutdown_mission: Optional[str] = None
    main_missions: List[str] = field(default_factory=list)

    phase: Optional[str] = None  # "setup" | "main" | "shutdown"
    current_mission: Optional[str] = None
    completed_missions: Set[str] = field(default_factory=set)

    step_name: Optional[str] = None
    step_pos: Optional[Tuple[int, int]] = None  # (n, total)
    substep_pos: Optional[Tuple[int, int]] = None  # (n, total) or None

    def update(self, rec) -> None:
        """Fold one record into the current state (duck-typed on ``.message``)."""
        msg = getattr(rec, "message", "") or ""
        if not msg:
            return

        m = _PRELOAD_MAIN.search(msg)
        if m:
            name = m.group(1)
            if name not in self.main_missions:
                self.main_missions.append(name)
            return
        m = _PRELOAD_SETUP.search(msg)
        if m:
            self.setup_mission = m.group(1)
            return
        m = _PRELOAD_SHUTDOWN.search(msg)
        if m:
            self.shutdown_mission = m.group(1)
            return

        m = _RUNNING_PHASE.search(msg)
        if m:
            self.phase = m.group(1)
            return

        m = _START_MISSION.search(msg)
        if m:
            self._enter_mission(m.group(1))
            return

        m = _COMPLETE_MISSION.search(msg)
        if m:
            self.completed_missions.add(m.group(1))
            return

        m = _STEP_MARKER.match(msg)
        if m:
            self.step_pos = (int(m.group(1)), int(m.group(2)))
            self.substep_pos = (
                (int(m.group(3)), int(m.group(4)))
                if m.group(3) is not None
                else None
            )
            self.step_name = m.group(5)

    def _enter_mission(self, name: str) -> None:
        # A repeated "Starting mission: X" (api logs it twice) is idempotent.
        if name == self.current_mission:
            return
        self.current_mission = name
        # Entering a new mission clears the step breadcrumb of the previous one.
        self.step_name = None
        self.step_pos = None
        self.substep_pos = None
        if name in self.main_missions:
            self.phase = "main"
        elif name == self.setup_mission:
            self.phase = "setup"
        elif name == self.shutdown_mission:
            self.phase = "shutdown"

    @property
    def mission_index(self) -> Optional[int]:
        """1-based position of the current mission among the *main* missions."""
        if self.current_mission in self.main_missions:
            return self.main_missions.index(self.current_mission) + 1
        return None

    @property
    def mission_total(self) -> Optional[int]:
        return len(self.main_missions) or None

    def mission_label(self) -> Optional[str]:
        """``M020CollectDrumsMission (2/5)`` — mission plus its main-run position."""
        if not self.current_mission:
            return None
        idx, total = self.mission_index, self.mission_total
        if self.phase == "main" and idx and total:
            return f"{self.current_mission} ({idx}/{total})"
        return self.current_mission

    def step_label(self) -> Optional[str]:
        """``10/20 › 3/4 CollectDrive`` — step position plus name, or ``None``."""
        if not self.step_name:
            return None
        parts: List[str] = []
        if self.step_pos:
            parts.append(f"{self.step_pos[0]}/{self.step_pos[1]}")
        if self.substep_pos:
            parts.append(f"› {self.substep_pos[0]}/{self.substep_pos[1]}")
        parts.append(self.step_name)
        return " ".join(parts)

    def breadcrumb(self) -> str:
        """One-line ``phase · mission · step`` summary (empty if nothing yet)."""
        segs: List[str] = []
        if self.phase:
            segs.append(self.phase)
        mission = self.mission_label()
        if mission:
            segs.append(mission)
        step = self.step_label()
        if step:
            segs.append(step)
        return " · ".join(segs)
