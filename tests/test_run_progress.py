"""Tests for the mission/step breadcrumb resolver (raccoon_cli.logs.progress)."""

from __future__ import annotations

from dataclasses import dataclass

from raccoon_cli.logs.progress import RunProgress


@dataclass
class _Rec:
    """Minimal duck-typed record (message + func) for feeding RunProgress."""

    message: str
    func: str = ""


def _feed(prog: RunProgress, *messages: str) -> None:
    for m in messages:
        prog.update(_Rec(m))


def test_preload_builds_mission_lists():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading setup mission: M000SetupMission",
        "Preloading main mission: M010DriveToDrumsMission",
        "Preloading main mission: M020CollectDrumsMission",
        "Preloading shutdown mission: M999ShutdownMission",
    )
    assert prog.setup_mission == "M000SetupMission"
    assert prog.shutdown_mission == "M999ShutdownMission"
    assert prog.main_missions == [
        "M010DriveToDrumsMission",
        "M020CollectDrumsMission",
    ]
    assert prog.mission_total == 2


def test_setup_phase_and_step():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading setup mission: M000SetupMission",
        "Running setup mission",
        "Starting mission: M000SetupMission",
        "4/20: StartCameraStep",
    )
    assert prog.phase == "setup"
    assert prog.current_mission == "M000SetupMission"
    assert prog.step_label() == "4/20 StartCameraStep"
    # A setup mission is not part of the main run → no (n/total) suffix.
    assert prog.mission_label() == "M000SetupMission"


def test_main_mission_index_and_breadcrumb():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading main mission: M010DriveToDrumsMission",
        "Preloading main mission: M020CollectDrumsMission",
        "Preloading main mission: M030DriveToPipeMission",
        "Starting mission: M020CollectDrumsMission",
        "10/20 > 3/4: CollectDrive(step=<...>, manual=True)",
    )
    assert prog.phase == "main"
    assert prog.mission_index == 2
    assert prog.mission_label() == "M020CollectDrumsMission (2/3)"
    assert prog.step_label() == "10/20 › 3/4 CollectDrive"
    assert prog.breadcrumb() == (
        "main · M020CollectDrumsMission (2/3) · 10/20 › 3/4 CollectDrive"
    )


def test_entering_new_mission_clears_step():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading main mission: M010DriveToDrumsMission",
        "Preloading main mission: M020CollectDrumsMission",
        "Starting mission: M010DriveToDrumsMission",
        "2/7: DriveForward",
        "Starting mission: M020CollectDrumsMission",
    )
    assert prog.current_mission == "M020CollectDrumsMission"
    assert prog.step_name is None
    assert prog.step_label() is None


def test_duplicate_start_is_idempotent():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading main mission: M010DriveToDrumsMission",
        "Starting mission: M010DriveToDrumsMission",
        "2/7: DriveForward",
        # api logs "Starting mission" twice; the repeat must not wipe the step.
        "Starting mission: M010DriveToDrumsMission",
    )
    assert prog.step_label() == "2/7 DriveForward"


def test_completed_missions_tracked():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading main mission: M010DriveToDrumsMission",
        "Starting mission: M010DriveToDrumsMission",
        "Completed mission: M010DriveToDrumsMission",
    )
    assert "M010DriveToDrumsMission" in prog.completed_missions


def test_shutdown_phase():
    prog = RunProgress()
    _feed(
        prog,
        "Preloading shutdown mission: M999ShutdownMission",
        "Running shutdown mission",
        "Starting mission: M999ShutdownMission",
        "1/1: StopCameraStep",
    )
    assert prog.phase == "shutdown"
    assert prog.step_label() == "1/1 StopCameraStep"


def test_empty_and_noise_messages_are_safe():
    prog = RunProgress()
    _feed(prog, "", "some unrelated debug line", "Wombat Motor port=2 getPosition")
    assert prog.breadcrumb() == ""
    assert prog.mission_label() is None
    assert prog.step_label() is None
