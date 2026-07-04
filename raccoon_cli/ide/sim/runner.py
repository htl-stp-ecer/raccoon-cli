"""Run a project's missions against the real libstp simulator.

Spawned as a subprocess by the IDE backend's ``simulate=real`` execution
path. Attaches a fresh ``SimWorld`` to the in-process MockPlatform, polls
the ground-truth pose on a timer, and streams it as newline-delimited JSON
to stdout while the project's ``src.main`` runs to completion.

Wire protocol (one JSON object per line, all event types include ``type``):
    {"type": "sim_started", "scene": "...", "start": [x, y, theta_deg]}
    {"type": "sim_pose", "t": 0.123, "x_cm": ..., "y_cm": ..., "theta_rad": ...}
    {"type": "sim_error", "message": "..."}
    {"type": "sim_finished", "exit_code": 0}

Any other stdout / stderr from the user program flows alongside on the
subprocess's normal streams — the IDE backend separates structured events
from free-form output by trying ``json.loads(line)`` first.

CLI::

    python -m raccoon_cli.ide.sim.runner \
        --project /path/to/project \
        --scene  empty_table.ftmap \
        --start  0,0,0 \
        --pose-hz 20
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _parse_start(raw: str | None) -> tuple[float, float, float]:
    if not raw:
        return (0.0, 0.0, 0.0)
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise SystemExit(f"--start must be 'x_cm,y_cm,theta_deg', got: {raw!r}")
    x, y, theta_deg = (float(p) for p in parts)
    return (x, y, math.radians(theta_deg))


def _resolve_scene(scene: str, project_root: Path) -> Path:
    candidate = Path(scene).expanduser()
    if candidate.is_absolute() and candidate.exists():
        return candidate
    # Project-local — the normal case: robot.physical.table_map is a
    # project-relative .ftmap path.
    local = project_root / candidate
    if local.exists():
        return local
    # Optional shared scene library (e.g. raccoon-lib/scenes for the built-in
    # empty_table fallback). No hard-coded path — point RACCOON_SIM_SCENE_DIR
    # (os.pathsep-separated) at it if you rely on bundled scenes.
    import os

    for entry in os.environ.get("RACCOON_SIM_SCENE_DIR", "").split(os.pathsep):
        if not entry.strip():
            continue
        bundled = Path(entry).expanduser() / candidate
        if bundled.exists():
            return bundled
    raise SystemExit(
        f"Scene not found: {scene!r}. Looked in {project_root} and "
        f"$RACCOON_SIM_SCENE_DIR. Configure robot.physical.table_map to a "
        f"project-relative .ftmap, or set RACCOON_SIM_SCENE_DIR."
    )


def _build_sim_robot_config(project_cfg: dict[str, Any]):
    """Translate raccoon.project.yml into a SimRobotConfig.

    Only the geometry/kinematics that the sim physics needs is touched; the
    rest of the project config (PID, sensors, etc.) is ignored.
    """
    from raccoon.testing.sim import SimRobotConfig

    robot = project_cfg.get("robot") or {}
    physical = robot.get("physical") or {}
    drive = robot.get("drive") or {}
    kin = drive.get("kinematics") or {}

    cfg = SimRobotConfig()
    if (w := physical.get("width_cm")) is not None:
        cfg.width_cm = float(w)
    if (length := physical.get("length_cm")) is not None:
        cfg.length_cm = float(length)
    rotc = physical.get("rotation_center") or {}
    if (rx := rotc.get("x_cm")) is not None:
        cfg.rotation_center_forward_cm = float(rx)
    if (ry := rotc.get("y_cm")) is not None:
        cfg.rotation_center_strafe_cm = float(ry)

    kind = (kin.get("type") or "differential").lower()
    if kind == "mecanum":
        cfg.drivetrain = "mecanum"
    else:
        cfg.drivetrain = "diff"

    if (wr := kin.get("wheel_radius")) is not None:
        cfg.wheel_radius_m = float(wr)
    if (wb := kin.get("wheelbase")) is not None:
        cfg.wheelbase_m = float(wb)
        cfg.track_width_m = float(wb)

    return cfg


def _start_pose_from_config_or_arg(
    project_cfg: dict[str, Any],
    cli_start: str | None,
) -> tuple[float, float, float]:
    if cli_start:
        return _parse_start(cli_start)
    physical = ((project_cfg.get("robot") or {}).get("physical") or {})
    sp = physical.get("start_pose") or {}
    x = float(sp.get("x_cm", 0.0))
    y = float(sp.get("y_cm", 0.0))
    theta = math.radians(float(sp.get("theta_deg", 0.0)))
    return (x, y, theta)


_current_mission_name: str | None = None
_PATH_INDEX_RE = re.compile(r"^(?:P\[)?(\d+)(?:/|\])")


def _path_segments_to_indices(segments: list[str]) -> list[int]:
    """Turn raccoon-lib's mixed path segments ('1/3', 'P[2/4]') into [1, 2]."""
    out: list[int] = []
    for seg in segments:
        match = _PATH_INDEX_RE.match(seg or "")
        if match:
            try:
                out.append(int(match.group(1)))
            except ValueError:
                continue
    return out


def _install_step_event_emitter() -> None:
    """Monkey-patch Step.run_step + Mission.run to stream highlight events.

    Emits one ``step`` event per *leaf* (non-composite) step so the IDE can
    light up the corresponding node in the flowchart. Composite steps
    (Sequential, Parallel) push path segments but don't run hardware
    themselves, so reporting them produces noise without any matching node.

    The current mission name is tracked through a module-level variable
    populated by patched ``Mission.run`` and attached to every step event,
    so the IDE can filter highlights to the visible mission.
    """
    from raccoon.step.base import Step, _step_path
    from raccoon.mission.api import Mission

    original_run_step = Step.run_step
    original_mission_run = Mission.run

    async def _patched_run_step(self, robot):  # type: ignore[no-untyped-def]
        if not getattr(self, "_composite", False):
            try:
                segments = list(_step_path.get() or [])
                path = _path_segments_to_indices(segments)
                signature = ""
                try:
                    signature = self._generate_signature()
                except Exception:
                    signature = type(self).__name__
                function_name = signature.split("(", 1)[0].strip() if signature else type(self).__name__
                _emit({
                    "type": "step",
                    "path": path,
                    "function_name": function_name,
                    "step_type": type(self).__name__,
                    "display_label": signature,
                    "mission_name": _current_mission_name,
                })
            except Exception:
                # Never let instrumentation break execution.
                pass
        await original_run_step(self, robot)

    async def _patched_mission_run(self, robot):  # type: ignore[no-untyped-def]
        global _current_mission_name
        name = type(self).__name__
        _current_mission_name = name
        _emit({"type": "mission_started", "mission_name": name})
        try:
            await original_mission_run(self, robot)
        finally:
            _emit({"type": "mission_finished", "mission_name": name})
            _current_mission_name = None

    Step.run_step = _patched_run_step  # type: ignore[assignment]
    Mission.run = _patched_mission_run  # type: ignore[assignment]


def _install_wait_for_button_bypass() -> None:
    """Replace ``WaitForButton._execute_step`` with an immediate no-op.

    Setup missions and several mission bodies block on physical button
    presses that don't exist in the sim. Without this, the all-missions
    run hangs on the first ``wait_for_button`` and no later mission runs.
    The IDE still emits a ``step`` event for the button so users see it
    was skipped, but execution proceeds.
    """
    try:
        from raccoon.step.wait_for_button import WaitForButton

        async def _noop(self, robot):  # type: ignore[no-untyped-def]
            return None

        WaitForButton._execute_step = _noop  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "sim_error",
               "message": f"Could not patch WaitForButton: {exc}"})


def _ensure_localization(robot: object) -> None:
    """Inject a default particle-filter Localization if the project didn't.

    Most generated robots ship odometry but no Localization (it's an opt-in
    in raccoon-lib). Motion steps now require ``robot.localization``, so
    every real-sim run would error 13ms in without this fallback. We mount
    a stock ``Localization(odometry, default config)`` so motion produces
    measurable pose changes — the user can still override by setting
    ``self._localization`` in their Robot subclass.
    """
    try:
        from raccoon.localization import Localization, LocalizationConfig
    except ImportError as exc:
        _emit({"type": "sim_error",
               "message": f"raccoon.localization missing: {exc}"})
        return

    if getattr(robot, "localization", None) is not None:
        return
    odometry = getattr(robot, "odometry", None)
    if odometry is None:
        _emit({"type": "sim_error",
               "message": "Robot has no odometry; motion steps will fail. "
                          "Configure robot.odometry in your project."})
        return
    try:
        robot._localization = Localization(odometry, LocalizationConfig())
    except Exception as exc:  # noqa: BLE001 — surface as structured event
        _emit({"type": "sim_error",
               "message": f"Failed to construct default Localization: {exc}"})


def _run_isolated_mission(mission_ref: str) -> None:
    """Build the project's Robot and run a single mission against the sim.

    Strips setup/shutdown missions and filters ``robot.missions`` to the
    requested one (matched by class name, case-insensitive) so users can
    drive a single mission under the simulator without the full project
    boot sequence.
    """
    import importlib

    robot_mod = importlib.import_module("src.hardware.robot")
    robot_cls = getattr(robot_mod, "Robot", None)
    if robot_cls is None:
        raise RuntimeError("src.hardware.robot does not export a 'Robot' class")

    robot = robot_cls()
    _ensure_localization(robot)

    # Drop setup/shutdown so only the user's mission runs. These are class
    # attributes on most generated robots, but we set them on the instance
    # so we don't mutate shared state.
    robot.setup_mission = None
    robot.shutdown_mission = None

    target = mission_ref.strip().lower()
    matched = []
    for m in list(getattr(robot, "missions", []) or []):
        name = type(m).__name__.lower()
        if name == target or name == f"{target}mission":
            matched.append(m)
    if not matched:
        _emit({
            "type": "sim_error",
            "message": f"Mission '{mission_ref}' not found on Robot. "
                       f"Available: {[type(m).__name__ for m in robot.missions]}",
        })
        return

    robot.missions = matched
    robot.start()


class _PosePoller(threading.Thread):
    """Background thread that emits ``sim_pose`` events at a fixed rate."""

    def __init__(self, hz: float, start_ts: float):
        super().__init__(daemon=True, name="sim-pose-poller")
        self._period = 1.0 / max(1.0, float(hz))
        self._start_ts = start_ts
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from raccoon.testing.sim import pose, yaw_rate
        while not self._stop.is_set():
            try:
                p = pose()
                _emit({
                    "type": "sim_pose",
                    "t": round(time.monotonic() - self._start_ts, 4),
                    "x_cm": float(p.x),
                    "y_cm": float(p.y),
                    "theta_rad": float(p.theta),
                    "yaw_rate": float(yaw_rate()),
                })
            except Exception as exc:  # noqa: BLE001 — best-effort sampling
                _emit({"type": "sim_pose_error", "message": str(exc)})
                break
            self._stop.wait(self._period)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raccoon-sim-runner")
    parser.add_argument("--project", required=True, type=Path,
                        help="Project root containing raccoon.project.yml")
    parser.add_argument("--scene", default="empty_table.ftmap",
                        help="Path to a .ftmap scene (project-relative or absolute)")
    parser.add_argument("--start", default=None,
                        help="Start pose as 'x_cm,y_cm,theta_deg'. "
                             "Defaults to robot.physical.start_pose.")
    parser.add_argument("--pose-hz", type=float, default=20.0,
                        help="Pose sampling rate in Hz (default 20).")
    parser.add_argument("--entry", default="src.main",
                        help="Python module to execute as the mission entry "
                             "(default 'src.main').")
    parser.add_argument("--mission", default=None,
                        help="If given, run only this mission (by class name "
                             "or order). Setup and shutdown missions are "
                             "stripped so the requested mission runs "
                             "isolated against the sim.")
    args = parser.parse_args(argv)

    project_root: Path = args.project.resolve()
    if not (project_root / "raccoon.project.yml").exists():
        raise SystemExit(f"No raccoon.project.yml in {project_root}")

    # Load config with !include resolution.
    from raccoon_cli.project import load_project_config
    project_cfg = load_project_config(project_root)

    scene_path = _resolve_scene(args.scene, project_root)
    sim_robot = _build_sim_robot_config(project_cfg)
    start_pose = _start_pose_from_config_or_arg(project_cfg, args.start)

    # Attach sim BEFORE importing the user's robot module so the MockPlatform
    # singleton is already pointing at a configured SimWorld when generated
    # hardware code instantiates motors/sensors.
    from raccoon.testing import sim as sim_api
    sim_api.configure(
        scene_path,
        robot=sim_robot,
        start=start_pose,
        auto_tick=True,
    )

    # GenericRobot._pre_start_gate() blocks every run on a button press unless
    # the project explicitly declared a `wait_for_light_sensor` in defs. The
    # sim has neither button nor light, so without this no mission ever
    # actually executes. Replace the gate with an immediate return so the
    # sim sees real motion. Same goes for the hardware health probe.
    try:
        from raccoon.robot.api import GenericRobot

        async def _noop_gate(self) -> None:  # type: ignore[no-untyped-def]
            return None

        GenericRobot._pre_start_gate = _noop_gate  # type: ignore[assignment]

        if hasattr(GenericRobot, "_run_platform_probe"):
            def _noop_probe(self) -> None:  # type: ignore[no-untyped-def]
                return None

            GenericRobot._run_platform_probe = _noop_probe  # type: ignore[assignment]

        # Inject default Localization at the start of every run, including
        # when the project's own src.main constructs Robot() and calls
        # start() — so the all-missions run path also benefits.
        _original_run_missions = GenericRobot._run_missions

        async def _run_missions_with_loc(self):  # type: ignore[no-untyped-def]
            _ensure_localization(self)
            await _original_run_missions(self)

        GenericRobot._run_missions = _run_missions_with_loc  # type: ignore[assignment]

        _install_step_event_emitter()
        _install_wait_for_button_bypass()
    except Exception as exc:  # noqa: BLE001 — non-fatal, just warn
        _emit({"type": "sim_error",
               "message": f"Could not patch GenericRobot hooks: {exc}"})

    _emit({
        "type": "sim_started",
        "scene": str(scene_path),
        "start": [start_pose[0], start_pose[1], math.degrees(start_pose[2])],
        "drivetrain": sim_robot.drivetrain,
    })

    # Make the project importable.
    project_str = str(project_root)
    if project_str not in sys.path:
        sys.path.insert(0, project_str)

    started_at = time.monotonic()
    poller = _PosePoller(hz=args.pose_hz, start_ts=started_at)
    poller.start()

    exit_code = 0
    try:
        if args.mission:
            _run_isolated_mission(args.mission)
        else:
            # Importing src.main as a top-level script triggers Robot().start().
            import runpy
            runpy.run_module(args.entry, run_name="__main__")
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException as exc:  # noqa: BLE001 — surface everything
        exit_code = 1
        _emit({
            "type": "sim_error",
            "message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
    finally:
        poller.stop()
        poller.join(timeout=1.0)
        try:
            sim_api.detach()
        except Exception:
            pass

    _emit({"type": "sim_finished", "exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
