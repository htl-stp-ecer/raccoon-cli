"""Shared simulation-runner orchestration for CLI and IDE.

Single source of truth for everything around spawning the libstp
simulator (``raccoon_cli.ide.sim.runner``):

  * which Python interpreter can import ``raccoon.testing.sim``
    (:func:`pick_sim_python`),
  * which scene a project simulates against — resolved from
    ``robot.physical.table_map`` in ``raccoon.project.yml``
    (:func:`resolve_simulation_settings`),
  * how the sim-runner subprocess command + environment are built
    (:func:`build_sim_runner_cmd`, :func:`build_sim_env`),
  * and a blocking smoke-test entry point used by ``raccoon upload``
    (:func:`run_sim_smoke`).

Both the CLI (``raccoon upload``) and the IDE backend
(``ide/services/mission_service.py``) call into this module so there is
exactly one code path — per the CLAUDE.md shared-service rule.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from raccoon_cli.table_map import TableMapVersionError, parse_v2

logger = logging.getLogger(__name__)

#: Scene used when a project declares no ``table_map`` (and no explicit
#: ``simulation.scene``). Only resolvable if it exists project-locally or
#: under ``RACCOON_SIM_SCENE_DIR`` — see ``runner._resolve_scene``.
DEFAULT_SCENE = "empty_table.ftmap"


# --------------------------------------------------------------------------- #
# Interpreter resolution
# --------------------------------------------------------------------------- #

# Cached for the process lifetime so the (up to three) probe subprocesses run
# at most once.
_sim_python_cache: str | None = None


def pick_sim_python() -> str | None:
    """Find a Python interpreter that can import ``raccoon.testing.sim``.

    Order of preference:
      1. ``RACCOON_SIM_PYTHON`` env var (explicit override)
      2. The interpreter running this process
      3. The system ``python3``

    Returns ``None`` if none work. The mock driver bundle
    (``DRIVER_BUNDLE=mock``) must be installed for the import to succeed;
    a plain Wombat-bundle wheel has no ``raccoon.sim.mock`` and fails.
    """
    global _sim_python_cache
    if _sim_python_cache:
        return _sim_python_cache

    candidates: list[str] = []
    explicit = os.environ.get("RACCOON_SIM_PYTHON")
    if explicit:
        candidates.append(explicit)
    candidates.append(sys.executable)
    sys_py = "/usr/bin/python3"
    if sys_py not in candidates:
        candidates.append(sys_py)

    # NOTE: raccoon-lib's MockPlatform crashes during interpreter shutdown
    # ("pure virtual method called") even on a clean import. Use os._exit
    # to skip Python's normal teardown and report success via exit code 0.
    probe = (
        "import os, raccoon.testing.sim as _s; "
        "assert hasattr(_s, 'SimRobotConfig'); "
        "os._exit(0)"
    )
    for cand in candidates:
        if not cand:
            continue
        try:
            result = subprocess.run(
                [cand, "-c", probe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            _sim_python_cache = cand
            logger.info("Real-sim interpreter resolved: %s", cand)
            return cand

    logger.warning(
        "Could not find a python interpreter with raccoon.testing.sim installed"
    )
    return None


# --------------------------------------------------------------------------- #
# Scene + start-pose resolution
# --------------------------------------------------------------------------- #


@dataclass
class SimSettings:
    """Resolved scene + start pose for a real-sim run."""

    scene: str
    start: dict[str, float] | None
    scene_source: str


def resolve_simulation_settings(
    project_root: Path,
    *,
    default_scene: str = DEFAULT_SCENE,
) -> SimSettings:
    """Pick scene + start pose for a real-sim run.

    Resolution order for the *scene* (first match wins):

      1. ``simulation.scene`` in ``raccoon.project.yml`` (explicit override).
      2. ``robot.physical.table_map`` — the same map the Web-IDE renders in
         the Table panel. Two sub-cases:
          a. A string → treat as a project-relative ``.ftmap`` path.
          b. An inline dict (``format/table/lines``) → materialize it to a
             temp ``.ftmap`` under ``.raccoon/sim/`` so the runner can hand a
             real path to ``WorldMap.load_ftmap``.
      3. *default_scene* (``empty_table.ftmap``).

    Start pose order: ``simulation.start_pose`` → ``robot.physical.start_pose``
    → (0,0,0). ``robot.physical.start_pose`` is left to the runner (which
    reads it from the config directly); this only surfaces an explicit
    ``simulation.start_pose`` override.
    """
    scene: str = default_scene
    start: dict[str, float] | None = None
    scene_source = "default"

    try:
        from raccoon_cli.project import load_project_config

        cfg = load_project_config(project_root)
    except Exception as exc:  # noqa: BLE001 — best-effort, fall back to default
        logger.debug("Could not load project config for sim settings: %s", exc)
        return SimSettings(scene=scene, start=None, scene_source=scene_source)

    sim_section = cfg.get("simulation") if isinstance(cfg, dict) else None
    explicit_scene = None
    if isinstance(sim_section, dict):
        raw = sim_section.get("scene")
        if isinstance(raw, str) and raw.strip():
            explicit_scene = raw.strip()
        sp = sim_section.get("start_pose")
        if isinstance(sp, dict):
            start = {
                "x_cm": float(sp.get("x_cm", 0.0)),
                "y_cm": float(sp.get("y_cm", 0.0)),
                "theta_deg": float(sp.get("theta_deg", 0.0)),
            }

    if explicit_scene:
        scene = explicit_scene
        scene_source = "simulation.scene"
    else:
        physical = (
            ((cfg.get("robot") or {}).get("physical") or {})
            if isinstance(cfg, dict)
            else {}
        )
        table_map = physical.get("table_map")
        if isinstance(table_map, str) and table_map.strip():
            scene = table_map.strip()
            scene_source = "robot.physical.table_map (path)"
        elif isinstance(table_map, dict) and table_map.get("lines") is not None:
            materialized = materialize_inline_ftmap(project_root, table_map)
            if materialized is not None:
                scene = str(materialized)
                scene_source = "robot.physical.table_map (inline)"

    return SimSettings(scene=scene, start=start, scene_source=scene_source)


def materialize_inline_ftmap(
    project_root: Path, table_map: dict[str, Any]
) -> Path | None:
    """Write an inline table_map dict to a temp ``.ftmap`` for the runner.

    Lives under ``.raccoon/sim/scene.ftmap`` inside the project so each run
    picks up the latest map without leaking files outside the project tree.
    We always rewrite — the file is cheap and stale data would silently
    desync from the Web-IDE's table editor.

    The runner is v2-only: the map is validated and written as canonical v2
    (layers + ramp transitions preserved, so multi-plane scenes simulate
    correctly). Legacy v1 maps (flat ``lines[]``) are rejected — the caller
    falls back to the default scene in that case.
    """
    try:
        payload = parse_v2(table_map)
    except TableMapVersionError as exc:
        logger.warning("Ignoring non-v2 inline table_map: %s", exc)
        return None
    try:
        sim_dir = project_root / ".raccoon" / "sim"
        sim_dir.mkdir(parents=True, exist_ok=True)
        target = sim_dir / "scene.ftmap"
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target
    except Exception as exc:  # noqa: BLE001 — non-fatal, caller falls back
        logger.warning("Could not materialize inline table_map: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Subprocess construction
# --------------------------------------------------------------------------- #


def toolchain_root() -> Path:
    """Repo root of this toolchain checkout (parent of ``raccoon_cli``)."""
    return Path(__file__).resolve().parents[1]


def build_sim_runner_cmd(
    python_exe: str,
    project_root: Path,
    scene: str,
    *,
    pose_hz: float = 20.0,
    mission: str | None = None,
    start: dict[str, float] | None = None,
) -> list[str]:
    """Build the ``raccoon_cli.ide.sim.runner`` subprocess argv."""
    cmd = [
        python_exe,
        "-m",
        "raccoon_cli.ide.sim.runner",
        "--project",
        str(project_root),
        "--scene",
        scene,
        "--pose-hz",
        str(pose_hz),
    ]
    if mission:
        cmd += ["--mission", mission]
    if start:
        cmd += ["--start", f"{start['x_cm']},{start['y_cm']},{start['theta_deg']}"]
    return cmd


def build_sim_env(
    base_env: dict[str, str] | None = None,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for the sim-runner subprocess.

    Disables dev-mode button gating (so the mission body actually executes
    instead of blocking on ``WaitForButton``), forces unbuffered output, and
    prepends this checkout's toolchain root to ``PYTHONPATH`` so the chosen
    interpreter sees *this* ``raccoon_cli`` rather than a stale site-packages
    copy that predates ``raccoon_cli.ide.sim``.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["LIBSTP_DEV_MODE"] = "0"
    env["LIBSTP_NO_CALIBRATE"] = "1"
    env["LIBSTP_NO_CHECKPOINTS"] = "1"

    root = str(toolchain_root())
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing_pp if existing_pp else "")

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# --------------------------------------------------------------------------- #
# Blocking smoke test (for `raccoon upload`)
# --------------------------------------------------------------------------- #


@dataclass
class SmokeResult:
    """Outcome of a blocking sim smoke test.

    ``ok`` is the gate: True only when the runner exited 0 *and* emitted no
    ``sim_error`` event. ``reason`` is a short machine-ish tag for the failure
    class (``ok``, ``no-mock-interpreter``, ``timeout``, ``sim-error``,
    ``nonzero-exit``, ``spawn-failed``).
    """

    ok: bool
    reason: str
    scene: str
    scene_source: str
    exit_code: int | None = None
    errors: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)


# Runner stdout lines with these event types are structured sim events, not
# user-program output.
_SIM_EVENT_TYPES = {
    "sim_started",
    "sim_pose",
    "sim_pose_error",
    "sim_error",
    "sim_finished",
    "step",
    "mission_started",
    "mission_finished",
}

_TAIL_MAX = 40


def run_sim_smoke(
    project_root: Path,
    *,
    timeout: float = 180.0,
    pose_hz: float = 5.0,
    on_line: Callable[[str], None] | None = None,
) -> SmokeResult:
    """Run the whole project once under the simulator as a pass/fail gate.

    Spawns the sim runner over ``src.main`` (no ``--mission`` filter) so the
    real boot sequence and every mission run exactly as they would on the
    robot — only the driver backend is the mock. Blocks until the runner
    exits or *timeout* elapses.

    The scene is resolved from ``robot.physical.table_map`` via
    :func:`resolve_simulation_settings`. If no mock-capable interpreter is
    found the result is ``ok=False`` with ``reason="no-mock-interpreter"`` —
    the gate fails hard rather than silently skipping.

    *on_line* receives every raw stdout/stderr line (structured events are
    passed through as their JSON text) for live echoing.
    """
    settings = resolve_simulation_settings(project_root)

    python_exe = pick_sim_python()
    if python_exe is None:
        return SmokeResult(
            ok=False,
            reason="no-mock-interpreter",
            scene=settings.scene,
            scene_source=settings.scene_source,
            errors=[
                "No Python interpreter can import raccoon.testing.sim. Install "
                "raccoon-lib with DRIVER_BUNDLE=mock, or set RACCOON_SIM_PYTHON "
                "to an interpreter that has it."
            ],
        )

    cmd = build_sim_runner_cmd(
        python_exe, project_root, settings.scene, pose_hz=pose_hz, start=settings.start
    )
    env = build_sim_env()

    errors: list[str] = []
    tail: list[str] = []
    saw_finished = False
    runner_exit: int | None = None

    def _record(line: str) -> None:
        if on_line is not None:
            on_line(line)
        tail.append(line)
        if len(tail) > _TAIL_MAX:
            del tail[0 : len(tail) - _TAIL_MAX]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return SmokeResult(
            ok=False,
            reason="spawn-failed",
            scene=settings.scene,
            scene_source=settings.scene_source,
            errors=[f"Could not start sim runner: {exc}"],
        )

    timed_out = False
    try:
        assert proc.stdout is not None
        import time as _time

        deadline = _time.monotonic() + timeout
        for line in proc.stdout:
            text = line.rstrip("\r\n")
            if not text:
                if _time.monotonic() > deadline:
                    timed_out = True
                    break
                continue

            # Parse structured sim events; classify sim_error / sim_finished.
            if text.startswith("{") and text.endswith("}"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("type") in _SIM_EVENT_TYPES:
                    etype = payload.get("type")
                    if etype == "sim_error":
                        msg = str(payload.get("message", "sim error"))
                        errors.append(msg)
                    elif etype == "sim_finished":
                        saw_finished = True
                        rc = payload.get("exit_code")
                        runner_exit = int(rc) if isinstance(rc, int) else None
                    _record(text)
                    if _time.monotonic() > deadline:
                        timed_out = True
                        break
                    continue

            _record(text)
            if _time.monotonic() > deadline:
                timed_out = True
                break

        if timed_out:
            proc.kill()
            proc.wait(timeout=5)
            return SmokeResult(
                ok=False,
                reason="timeout",
                scene=settings.scene,
                scene_source=settings.scene_source,
                exit_code=None,
                errors=errors
                + [f"Simulation did not finish within {timeout:.0f}s."],
                tail=list(tail),
            )

        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    process_exit = proc.returncode
    # Prefer the runner's own ``sim_finished`` exit_code (it reflects the user
    # program's exit), falling back to the process return code.
    effective_exit = runner_exit if runner_exit is not None else process_exit

    if errors:
        reason = "sim-error"
        ok = False
    elif not saw_finished:
        reason = "no-finish"
        ok = False
        errors.append(
            "Simulation ended without a sim_finished event (runner crashed "
            "before completing)."
        )
    elif effective_exit not in (0, None):
        reason = "nonzero-exit"
        ok = False
    else:
        reason = "ok"
        ok = True

    return SmokeResult(
        ok=ok,
        reason=reason,
        scene=settings.scene,
        scene_source=settings.scene_source,
        exit_code=effective_exit,
        errors=errors,
        tail=list(tail),
    )
