"""Run command for raccoon CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import os
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from raccoon_cli.checkpoint import create_checkpoint
from raccoon_cli.codegen import create_pipeline
from raccoon_cli.project import ProjectError, load_project_config, require_project
from raccoon_cli.commands.codegen import _resolve_ftmap_paths
from raccoon_cli.commands.migrate import _get_format_version, _load_migrations
from raccoon_cli.run_configurations import (
    RunConfiguration,
    load_run_configurations,
)
from raccoon_cli.run_recording import (
    build_run_env,
    make_run_id,
    prune_runs,
    write_run_manifest,
)

logger = logging.getLogger("raccoon")

_NO_MISSION_RE = re.compile(r"^--no-m(\d+)$")
_ACTIVE_PROGRAM_LOCK_PATH = Path.home() / ".raccoon" / "active_program.lock"
_ACTIVE_PROGRAM_STATE_PATH = Path.home() / ".raccoon" / "active_program.json"

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def _extract_run_config(
    args: tuple, available: dict
) -> tuple[tuple, RunConfiguration | None]:
    """If the first arg matches a known run-configuration name, pop and return it.

    Names are matched case-insensitively so ``raccoon run Dev`` works.
    """
    if not args:
        return args, None
    candidate = args[0]
    if candidate.startswith("-"):
        return args, None
    lower = candidate.lower()
    for cfg_name, cfg in available.items():
        if cfg_name.lower() == lower:
            return args[1:], cfg
    return args, None


def _extract_skip_missions(args: tuple) -> tuple[tuple, set[int]]:
    """Pull --no-mN flags out of args; return (remaining_args, skip_indices).

    For example, ``("--no-m0", "--no-m2", "foo")`` → ``(("foo",), {0, 2})``.
    """
    remaining = []
    skip: set[int] = set()
    for arg in args:
        m = _NO_MISSION_RE.match(arg)
        if m:
            skip.add(int(m.group(1)))
        else:
            remaining.append(arg)
    return tuple(remaining), skip


def _missions_from_args(args: tuple) -> list[str]:
    """Positional (non-flag) args a user passed to ``raccoon run`` — the missions.

    ``raccoon run M050 M060`` → ``["M050", "M060"]``. Flags (``--dev`` etc.) and
    their values are already stripped upstream, so anything not starting with
    ``-`` is a mission selector recorded in the manifest.
    """
    return [a for a in args if not a.startswith("-")]


def _announce_run_dir(
    console: Console,
    run_dir: Path,
    *,
    remote: bool,
    record_localization: bool,
    profile: bool,
) -> None:
    """Tell the user where artifacts land and what's being recorded."""
    where = "on Pi → " if remote else ""
    parts = ["log"]
    if record_localization:
        parts.append("localization")
    if profile:
        parts.append("profile")
    console.print(
        f"[cyan]Run artifacts {where}{run_dir}[/cyan] "
        f"[dim](recording: {', '.join(parts)})[/dim]"
    )


_WARN_ERROR_RE = re.compile(r"\b(WARNING|WARN|ERROR|CRITICAL|FATAL)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _is_warn_or_error(line: str) -> bool:
    return bool(_WARN_ERROR_RE.search(_ANSI_RE.sub("", line)))


def _has_error_lines(collected: list[str]) -> bool:
    """Return True if any collected line is an actual error-level line."""
    for line in collected:
        clean = _ANSI_RE.sub("", line)
        if _ERROR_RE.search(clean):
            return True
    return False


def _print_service_deployments(console: Console, deployments: list[dict]) -> None:
    """Render a per-service summary of what the Pi did during service deploy."""
    if not deployments:
        return
    from rich.table import Table

    table = Table(
        title="[bold]Project services[/bold]",
        show_header=True,
        title_style="bold",
        padding=(0, 1),
        box=box.SIMPLE,
    )
    table.add_column("Service", style="cyan")
    table.add_column("Action")
    table.add_column("Reason", style="dim")

    for d in deployments:
        action = d.get("action", "?")
        if action == "restart":
            action_text = Text("restarted", style="bold yellow")
        elif d.get("digest_changed"):
            action_text = Text("started", style="green")
        else:
            action_text = Text("unchanged", style="dim")
        if d.get("first_deploy"):
            action_text = Text("installed", style="bold green")
        table.add_row(d.get("name", "?"), action_text, d.get("reason", ""))

    console.print(table)


def _print_output_summary(console: Console, collected: list[str]) -> None:
    """Print collected warning/error lines from program output as a summary panel."""
    if not collected:
        return
    text = Text(overflow="ellipsis", no_wrap=True)
    for line in collected:
        clean = _ANSI_RE.sub("", line)
        style = "bold red" if _ERROR_RE.search(clean) else "bold yellow"
        text.append(clean + "\n", style=style)
    console.print(
        Panel(
            text,
            title=f"[bold yellow]Program Warnings & Errors ({len(collected)})[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
            expand=True,
        )
    )


def _terminate_process_on_interrupt(proc: subprocess.Popen, console: Console) -> int:
    """Stop a child process after Ctrl+C and return its exit code."""
    console.print("\n[yellow]Ctrl+C — stopping program...[/yellow]")
    return _terminate_program_process(proc)


@contextmanager
def _active_program_lock():
    """Serialize program start/stop decisions across raccoon run processes."""
    _ACTIVE_PROGRAM_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _ACTIVE_PROGRAM_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_active_program_state() -> dict | None:
    """Load the last known active program record, if any."""
    try:
        return json.loads(_ACTIVE_PROGRAM_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Ignoring unreadable active program state", exc_info=True)
        return None


def _write_active_program_state(*, pid: int, project_root: Path, cmd_parts: list[str]) -> None:
    """Persist the currently running robot program."""
    _ACTIVE_PROGRAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_PROGRAM_STATE_PATH.write_text(
        json.dumps(
            {
                "pid": pid,
                "project_root": str(project_root.resolve()),
                "cmd_parts": list(cmd_parts),
            }
        ),
        encoding="utf-8",
    )


def _clear_active_program_state(pid: int) -> None:
    """Clear the active program record if it still points at pid."""
    state = _read_active_program_state()
    if not state or state.get("pid") != pid:
        return
    with suppress(FileNotFoundError):
        _ACTIVE_PROGRAM_STATE_PATH.unlink()


def _is_process_alive(pid: int) -> bool:
    """Return True when pid still exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _list_robot_program_pids() -> list[int]:
    """Find live `python -m src.main` processes on Linux via /proc."""
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []

    matches: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline_raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmdline_raw:
            continue
        argv = [part.decode("utf-8", errors="ignore") for part in cmdline_raw.split(b"\0") if part]
        if len(argv) < 3:
            continue
        if "-m" not in argv:
            continue
        try:
            module = argv[argv.index("-m") + 1]
        except (ValueError, IndexError):
            continue
        if module != "src.main":
            continue
        matches.append(pid)
    return matches


def _kill_process_group(pid: int, sig: int) -> None:
    """Send sig to the process tree rooted at pid."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, OSError):
            pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _wait_for_process_exit(pid: int, timeout_s: float) -> bool:
    """Wait for pid to disappear."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            return True
        time.sleep(0.1)
    return not _is_process_alive(pid)


def _terminate_process_by_pid(pid: int) -> None:
    """Terminate a stale robot program by pid."""
    _kill_process_group(pid, signal.SIGTERM)
    if _wait_for_process_exit(pid, timeout_s=3.0):
        return
    _kill_process_group(pid, signal.SIGKILL)
    _wait_for_process_exit(pid, timeout_s=1.0)


def _terminate_program_process(proc: subprocess.Popen) -> int:
    """Terminate a spawned src.main process and return its exit code."""
    pid = proc.pid
    _kill_process_group(pid, signal.SIGTERM)
    try:
        return proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        _kill_process_group(pid, signal.SIGKILL)
        return proc.wait()


def _ensure_single_active_program(project_root: Path, console: Console) -> None:
    """Kill any previously active robot program before launching a new one."""
    stale_pids: set[int] = set()

    state = _read_active_program_state()
    if state:
        pid = state.get("pid")
        if isinstance(pid, int) and pid > 0 and _is_process_alive(pid):
            stale_pids.add(pid)

    stale_pids.update(_list_robot_program_pids())
    stale_pids.discard(os.getpid())

    if not stale_pids:
        return

    console.print(
        f"[yellow]Stopping existing robot program(s): {', '.join(str(pid) for pid in sorted(stale_pids))}[/yellow]"
    )
    for pid in sorted(stale_pids):
        _terminate_process_by_pid(pid)


def _install_termination_handlers():
    """Forward SIGINT/SIGTERM to the child process group."""
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def _handler(sig, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return original_sigint, original_sigterm


def _restore_termination_handlers(original_sigint, original_sigterm) -> None:
    """Restore previous SIGINT/SIGTERM handlers."""
    signal.signal(signal.SIGINT, original_sigint)
    signal.signal(signal.SIGTERM, original_sigterm)


def _run_via_pty(
    cmd_parts: list[str],
    project_root: Path,
    env: dict,
    console: Console,
) -> int:
    """Run cmd_parts with a PTY as stdout/stderr so isatty() returns True.

    Used when our own stdout is a pipe (Pi server executor).  The PTY makes
    colour libraries that check isatty() activate, and the output is relayed
    byte-for-byte to sys.stdout so the executor can forward it upstream.
    """
    import pty
    import select
    import termios

    master_fd, slave_fd = pty.openpty()

    # Disable output-processing on the slave so the PTY line discipline does
    # not convert bare \n → \r\n before we relay the bytes.
    attrs = termios.tcgetattr(slave_fd)
    attrs[1] &= ~termios.OPOST  # c_oflag: clear OPOST
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

    with _active_program_lock():
        _ensure_single_active_program(project_root, console)
        proc = subprocess.Popen(
            cmd_parts,
            cwd=project_root,
            env=env,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=(os.name == "posix"),
        )
        _write_active_program_state(
            pid=proc.pid,
            project_root=project_root,
            cmd_parts=cmd_parts,
        )
    os.close(slave_fd)  # parent no longer needs the slave end
    original_sigint, original_sigterm = _install_termination_handlers()

    returncode: int | None = None
    try:
        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.05)
            except (ValueError, OSError):
                break
            if r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except OSError:
                    # EIO: child closed the slave (normal exit path)
                    break
            elif proc.poll() is not None:
                # Process exited; drain any remaining PTY bytes
                try:
                    while True:
                        r2, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r2:
                            break
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                except OSError:
                    pass
                break
    except KeyboardInterrupt:
        returncode = _terminate_process_on_interrupt(proc, console)
    finally:
        _restore_termination_handlers(original_sigint, original_sigterm)
        with _active_program_lock():
            _clear_active_program_state(proc.pid)
        try:
            os.close(master_fd)
        except OSError:
            pass

    if returncode is not None:
        return returncode
    return proc.wait()


def _run_local_with_tui(
    cmd_parts: list[str],
    project_root: Path,
    env: dict,
    console: Console,
    log_path: Path | None = None,
) -> int:
    """Run the child with its stdout suppressed and stream its JSONL log live.

    The child writes its run log to ``<run_dir>/libstp.jsonl`` with full detail
    and only warn/error to stdout; we send stdout to /dev/null and render the
    JSONL in a live TUI instead. stderr is captured to a temp file (a pipe could
    deadlock if a traceback fills the buffer) and its tail is shown on a non-zero
    exit.

    When *log_path* is given (the exact ``<run_dir>/libstp.jsonl`` this run will
    write), the streamer tails it directly — race-free, no newest-file discovery.
    """
    import tempfile

    from raccoon_cli.logs.live_stream import stream_run_logs

    runs_dir = project_root / ".raccoon" / "runs"
    title = project_root.name

    err_file = tempfile.TemporaryFile(mode="w+b")
    with _active_program_lock():
        _ensure_single_active_program(project_root, console)
        proc = subprocess.Popen(
            cmd_parts,
            cwd=project_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err_file,
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        _write_active_program_state(
            pid=proc.pid,
            project_root=project_root,
            cmd_parts=cmd_parts,
        )

    original_sigint, original_sigterm = _install_termination_handlers()
    returncode: int | None = None
    streamed = False
    try:
        try:
            streamed = stream_run_logs(
                runs_dir,
                is_running=lambda: proc.poll() is None,
                console=console,
                title=title,
                log_path=log_path,
            )
        except Exception as exc:  # never let a TUI glitch kill the run
            console.print(f"[yellow]Live log view unavailable ({exc}); waiting…[/yellow]")
        returncode = proc.wait()
    except KeyboardInterrupt:
        returncode = _terminate_process_on_interrupt(proc, console)
    finally:
        _restore_termination_handlers(original_sigint, original_sigterm)
        with _active_program_lock():
            _clear_active_program_state(proc.pid)

    if not streamed:
        console.print(
            "[dim]No .raccoon/runs/*/libstp.jsonl appeared for this run "
            "(use [cyan]--raw[/cyan] to see the program's stdout directly).[/dim]"
        )

    # Surface a crash: dump the tail of captured stderr on a bad exit.
    if returncode not in (0, None):
        try:
            err_file.seek(0)
            err = err_file.read().decode("utf-8", errors="replace").strip()
        except Exception:
            err = ""
        if err:
            tail = "\n".join(err.splitlines()[-30:])
            console.print(
                Panel(
                    Text(tail, style="red"),
                    title="stderr (tail)",
                    border_style="red",
                )
            )
    with suppress(Exception):
        err_file.close()

    return returncode if returncode is not None else 0


def _on_raspberry_pi() -> bool:
    """True when running on the robot (ARM), where run dirs accumulate."""
    import platform

    return platform.machine() in ("aarch64", "arm64", "armv7l")


def _sensor_rings_present() -> bool:
    """True when the stm32-data-reader SHM rings exist (so we can record)."""
    import glob

    return bool(glob.glob("/dev/shm/raccoon_ring_*"))


def _start_sensor_recorder(run_dir: Path, env: dict, console: Console):
    """Spawn the run-scoped SHM sensor recorder; return the process or None.

    Only starts when the sensor rings are present (i.e. we are on the robot
    with the reader running). Shares the run's process group so a killpg from
    the Pi server also reaches it — but we stop it explicitly too.
    """
    if not _sensor_rings_present():
        console.print(
            "[dim]Sensor recording requested but no SHM rings found "
            "(reader not running?) — skipping.[/dim]"
        )
        return None
    out_path = run_dir / "sensors.mcap"
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "raccoon_cli.logs.sensor_recorder",
                "--out", str(out_path),
                "--preset", "default",
            ],
            env=env,
        )
    except Exception as exc:  # pragma: no cover - defensive
        console.print(f"[yellow]Could not start sensor recorder: {exc}[/yellow]")
        return None
    console.print(f"[dim]Recording sensors → {out_path.name}[/dim]")
    return proc


def _stop_sensor_recorder(proc, console: Console) -> None:
    """SIGTERM the recorder and wait for it to finalise the MCAP index."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()  # SIGTERM → recorder finishes the MCAP file
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception:  # pragma: no cover - defensive
        pass


def _run_local(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    args: tuple,
    dev: bool = False,
    no_calibrate: bool = False,
    no_codegen: bool = False,
    no_checkpoints: bool = False,
    debug: bool = False,
    skip_missions: set[int] | None = None,
    record_localization: bool = False,
    profile: bool = True,
    record_hz: float | None = None,
    record_sensors: bool = True,
    extra_env: dict | None = None,
    raw: bool = False,
    run_id: str | None = None,
) -> None:
    """Run the project locally."""
    console: Console = ctx.obj["console"]
    if run_id is None:
        run_id = make_run_id()

    if config.get("auto_checkpoints", True):
        result = create_checkpoint(project_root, label="pre-run")
        if result.created:
            console.print(f"[dim]Checkpoint {result.short_sha} saved[/dim]")

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if not no_codegen:
        pipeline = create_pipeline()
        output_dir = project_root / "src" / "hardware"
        pipeline.run_all(
            _resolve_ftmap_paths(config, project_root), output_dir, format_code=True
        )

    env = os.environ.copy()
    # For a remote run the laptop launches `raccoon run --local` on the Pi and
    # passes RACCOON_RECORD_SENSORS in the env. The Pi's fresh invocation would
    # otherwise fall back to the flag default (on); honour the inherited opt-out.
    _inherited_rs = env.get("RACCOON_RECORD_SENSORS")
    effective_record_sensors = (
        record_sensors if _inherited_rs is None else _inherited_rs == "1"
    )
    env["PYTHONUNBUFFERED"] = "1"
    # Force color output: uv and other launchers may not propagate the TTY to
    # the child Python process, so libraries like Rich fall back to no-color mode.
    env.setdefault("FORCE_COLOR", "1")
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    # Ensure ~/.local/bin is in PATH so uv and other user-installed tools are found
    local_bin = str(Path.home() / ".local" / "bin")
    path_dirs = env.get("PATH", "").split(os.pathsep)
    if local_bin not in path_dirs:
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

    if (project_root / "pyproject.toml").exists():
        import platform
        import shutil

        uv = shutil.which("uv", path=env["PATH"])
        # On ARM (Pi), raccoon is pre-installed in the server's Python env.
        # uv would try to create a fresh venv using pyproject.toml sources
        # that contain laptop-specific absolute paths → always fails.
        if uv and platform.machine() not in ("aarch64", "arm64", "armv7l"):
            cmd_parts = [uv, "run", "start", *args]
            logger.info("pyproject.toml found — using uv run start")
        else:
            cmd_parts = [sys.executable, "-m", "src.main", *args]
            logger.info("pyproject.toml found — using sys.executable -m src.main (ARM/no-uv path)")
    else:
        cmd_parts = [sys.executable, "-m", "src.main", *args]
    logger.info(f"Executing: {' '.join(cmd_parts)}")
    if dev:
        env["LIBSTP_DEV_MODE"] = "1"
    if no_calibrate:
        env["LIBSTP_NO_CALIBRATE"] = "1"
    if no_checkpoints:
        env["LIBSTP_NO_CHECKPOINTS"] = "1"
    if debug:
        env["LIBSTP_DEBUG"] = "1"
    if skip_missions:
        env["LIBSTP_SKIP_MISSIONS"] = ",".join(str(i) for i in sorted(skip_missions))
    if extra_env:
        # Run-configuration env vars override the inherited environment but
        # not the LIBSTP_* flags we just set explicitly above — those reflect
        # the resolved CLI/config combination.
        for key, value in extra_env.items():
            env.setdefault(key, str(value))

    # Unified per-run artifact dir: write the manifest, then point raccoon-lib's
    # log / localization / profile writers at .raccoon/runs/<run_id>/. The log
    # dir is absolute so it's robust regardless of the child's cwd.
    run_dir = write_run_manifest(
        project_root,
        run_id,
        missions=_missions_from_args(args),
        args=list(args),
        record_localization=record_localization,
        profile=profile,
        record_sensors=effective_record_sensors,
        project=config.get("name") if isinstance(config, dict) else None,
    )
    for key, value in build_run_env(
        run_id,
        absolute=True,
        project_path=project_root,
        record_localization=record_localization,
        profile=profile,
        record_hz=record_hz,
        record_sensors=effective_record_sensors,
    ).items():
        env[key] = value
    _announce_run_dir(
        console, run_dir, remote=False,
        record_localization=record_localization, profile=profile,
    )
    run_log_path = run_dir / "libstp.jsonl"

    # Retention (Pi only): keep the 3 newest run dirs, delete older ones so the
    # SD card does not fill up with raw sensor recordings. The just-created run
    # dir counts as newest and is always kept. Best-effort — never fail the run.
    if _on_raspberry_pi():
        try:
            removed = prune_runs(project_root, keep=3)
            if removed:
                console.print(
                    f"[dim]Pruned {len(removed)} old run(s): "
                    f"{', '.join(removed)}[/dim]"
                )
        except Exception:
            logger.exception("Run retention prune failed")

    # On Windows, Ctrl+C doesn't reliably propagate to child processes.
    # Use Popen so we can catch SIGINT ourselves and terminate the child.
    #
    # When stdout is already a TTY (interactive console), inherit it so the
    # child writes directly to the terminal.  When stdout is a pipe (e.g. the
    # Pi server executor captures output), allocate a PTY so the child's
    # isatty() check returns True and colour libraries (loguru, colorama, …)
    # activate — FORCE_COLOR alone only helps libraries that check that env var.
    # Prefer the live JSONL TUI on an interactive terminal: the child's stdout
    # now carries only warn/error, while the full run detail is streamed from
    # .raccoon/runs/<run_id>/libstp.jsonl into a scrolling viewer. --raw (or a
    # non-TTY) falls back to inheriting the child's stdout directly.
    use_tui = sys.stdout.isatty() and not raw and os.environ.get("RACCOON_RUN_RAW") != "1"

    # Record raw SHM sensor channels for the lifetime of the mission (Pi only —
    # the rings live on the robot). Started before and stopped after whichever
    # launch path runs, so the MCAP file is always finalised.
    sensor_recorder = None
    if effective_record_sensors:
        sensor_recorder = _start_sensor_recorder(run_dir, env, console)

    try:
        if use_tui:
            returncode = _run_local_with_tui(
                cmd_parts, project_root, env, console, log_path=run_log_path
            )
        elif sys.platform == "win32" or sys.stdout.isatty():
            with _active_program_lock():
                _ensure_single_active_program(project_root, console)
                proc = subprocess.Popen(
                    cmd_parts,
                    cwd=project_root,
                    env=env,
                    start_new_session=(os.name == "posix"),
                )
                _write_active_program_state(
                    pid=proc.pid,
                    project_root=project_root,
                    cmd_parts=cmd_parts,
                )
            original_sigint, original_sigterm = _install_termination_handlers()
            try:
                returncode = proc.wait()
            except KeyboardInterrupt:
                returncode = _terminate_process_on_interrupt(proc, console)
            finally:
                _restore_termination_handlers(original_sigint, original_sigterm)
                with _active_program_lock():
                    _clear_active_program_state(proc.pid)
        else:
            returncode = _run_via_pty(cmd_parts, project_root, env, console)
    finally:
        _stop_sensor_recorder(sensor_recorder, console)

    exit_style = "bold green" if returncode == 0 else "bold red"
    console.print(
        Panel.fit(
            Text(f"src.main exited with code {returncode}", style=exit_style),
            border_style="green" if returncode == 0 else "red",
        )
    )

    if returncode != 0:
        raise SystemExit(returncode)


async def _ping_until_ready(
    host: str, console: Console, attempts: int = 6, interval: float = 0.4
) -> bool:
    """Ping host until it responds, warming up ARP cache before connecting."""
    import platform

    flag = "-n" if platform.system() == "Windows" else "-c"
    success = 0
    for _ in range(attempts):
        proc = await asyncio.create_subprocess_exec(
            "ping",
            flag,
            "1",
            "-W",
            "1",
            host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            success += 1
            if success >= 2:
                return True
        await asyncio.sleep(interval)
    return False


async def _run_remote(
    ctx: click.Context,
    project_root: Path,
    config: dict,
    args: tuple,
    dev: bool = False,
    no_calibrate: bool = False,
    no_codegen: bool = False,
    no_sync: bool = False,
    no_checkpoints: bool = False,
    debug: bool = False,
    skip_missions: set[int] | None = None,
    record_localization: bool = False,
    profile: bool = True,
    record_hz: float | None = None,
    record_sensors: bool = True,
    extra_env: dict | None = None,
    run_id: str | None = None,
) -> None:
    """Run the project on the connected Pi."""
    console: Console = ctx.obj["console"]
    if run_id is None:
        run_id = make_run_id()

    if config.get("auto_checkpoints", True):
        result = create_checkpoint(project_root, label="pre-run")
        if result.created:
            console.print(f"[dim]Checkpoint {result.short_sha} saved[/dim]")

    from raccoon_cli.client.connection import get_connection_manager
    from raccoon_cli.client.api import create_api_client
    from raccoon_cli.client.output_handler import OutputHandler
    from raccoon_cli.client.sftp_sync import SyncDirection
    from raccoon_cli.commands.sync_cmd import sync_project_interactive

    # Run codegen locally before syncing so generated files are included
    if not no_codegen:
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        pipeline = create_pipeline()
        output_dir = project_root / "src" / "hardware"
        pipeline.run_all(
            _resolve_ftmap_paths(config, project_root), output_dir, format_code=True
        )

    # Sync project to Pi before running
    if no_sync:
        console.print("[yellow]Skipping pre-run sync (--no-sync).[/yellow]")
        console.print()
    else:
        if not sync_project_interactive(project_root, console):
            console.print("[red]Sync failed, cannot run remotely[/red]")
            raise SystemExit(1)
        console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[dim]Checking connectivity to {state.pi_address}...[/dim]")
    if not await _ping_until_ready(state.pi_address, console):
        console.print(
            f"[yellow]Warning: {state.pi_address} not responding to ping — trying anyway[/yellow]"
        )

    console.print(f"[cyan]Running '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Start the run command on Pi
    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            env = {}
            if dev:
                env["LIBSTP_DEV_MODE"] = "1"
            if no_calibrate:
                env["LIBSTP_NO_CALIBRATE"] = "1"
            if no_checkpoints:
                env["LIBSTP_NO_CHECKPOINTS"] = "1"
            if debug:
                env["LIBSTP_DEBUG"] = "1"
            if skip_missions:
                env["LIBSTP_SKIP_MISSIONS"] = ",".join(
                    str(i) for i in sorted(skip_missions)
                )
            if extra_env:
                for key, value in extra_env.items():
                    env.setdefault(key, str(value))
            # Unified per-run artifact dir. The Pi writes into the synced
            # project's .raccoon/runs/<run_id>/ (relative paths — the abs
            # laptop path is meaningless there); we pre-create the same dir
            # locally with the manifest so the pull-back sync merges cleanly.
            run_dir = write_run_manifest(
                project_root,
                run_id,
                missions=_missions_from_args(args),
                args=list(args),
                record_localization=record_localization,
                profile=profile,
                record_sensors=record_sensors,
                project=project_name,
            )
            for key, value in build_run_env(
                run_id,
                absolute=False,
                record_localization=record_localization,
                profile=profile,
                record_hz=record_hz,
                record_sensors=record_sensors,
            ).items():
                env[key] = value
            _announce_run_dir(
                console, run_dir, remote=True,
                record_localization=record_localization, profile=profile,
            )
            result = await client.run_project(project_uuid, args=list(args), env=env)
        except Exception as e:
            console.print(f"[red]Failed to start run on Pi: {e}[/red]")
            raise SystemExit(1)

        _print_service_deployments(console, result.service_deployments or [])

        # Stream output via WebSocket (URL includes auth token)
        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print("[dim]Press Ctrl+C to stop[/dim]")
        console.print()

        # Handle Ctrl+C to cancel the remote command
        cancel_requested = False

        def signal_handler(sig, frame):
            nonlocal cancel_requested
            if not cancel_requested:
                cancel_requested = True
                console.print("\n[yellow]Cancelling...[/yellow]")
                handler.cancel()

        original_handler = signal.signal(signal.SIGINT, signal_handler)

        collected: list[str] = []

        def _collect_line(line: str) -> None:
            if _is_warn_or_error(line):
                collected.append(line)

        try:
            final_status = handler.stream_to_console(console, on_line=_collect_line)
        finally:
            signal.signal(signal.SIGINT, original_handler)

        _print_output_summary(console, collected)

        # Sync changes back from Pi (preserve locally-edited files)
        console.print()
        console.print("[dim]Syncing changes from Pi...[/dim]")
        sync_project_interactive(
            project_root, console, direction=SyncDirection.PULL, update=True
        )

        # Display final status
        exit_code = final_status.get("exit_code", -1)
        status = final_status.get("status", "unknown")
        success = exit_code == 0

        if exit_code != 0 and collected and not _has_error_lines(collected):
            console.print(
                "[yellow]Non-zero remote exit code returned, but output contained only warnings; "
                "treating run as successful.[/yellow]"
            )
            exit_code = 0
            status = "completed"
            success = True

        exit_style = "bold green" if success else "bold red"
        console.print()
        console.print(
            Panel.fit(
                Text(
                    f"Remote execution {status} with code {exit_code}", style=exit_style
                ),
                border_style="green" if success else "red",
            )
        )

        if exit_code != 0:
            raise SystemExit(exit_code)


def _warn_if_migrations_pending(console: Console, project_root: Path) -> None:
    try:
        current = _get_format_version(project_root)

        # Check lib's minimum requirement first — this is a hard blocker.
        try:
            import raccoon as _raccoon_lib

            lib_min = getattr(_raccoon_lib, "MIN_FORMAT_VERSION", None)
            if lib_min is not None and current < lib_min:
                console.print(
                    f"[bold red]✗  raccoon-lib requires format_version≥{lib_min} "
                    f"but this project is at format_version={current}.[/bold red]\n"
                    f"   Run [cyan]raccoon migrate[/cyan] before running."
                )
                raise SystemExit(1)
        except SystemExit:
            raise
        except Exception:
            pass

        # Soft warning: CLI has unapplied migrations.
        migrations = _load_migrations()
        if not migrations:
            return
        latest = migrations[-1].NUMBER
        if current < latest:
            pending_count = sum(1 for m in migrations if current < m.NUMBER)
            console.print(
                f"[bold yellow]⚠  Project format is out of date "
                f"(format_version={current}, latest={latest}, "
                f"{pending_count} migration(s) pending).[/bold yellow]\n"
                f"   Run [cyan]raccoon migrate[/cyan] to update."
            )
    except SystemExit:
        raise
    except Exception:
        pass


@click.command(
    name="run",
    context_settings=dict(allow_extra_args=True, ignore_unknown_options=True),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--dev", is_flag=True, help="Dev mode: use button instead of wait-for-light"
)
@click.option("--local", "-l", is_flag=True, help="Force local execution (skip remote)")
@click.option("--no-sync", is_flag=True, help="Skip syncing before remote run")
@click.option(
    "--no-calibrate", is_flag=True, help="Skip calibration steps, use stored values"
)
@click.option(
    "--no-codegen",
    is_flag=True,
    help="Skip code generation (used by server when codegen was done client-side)",
)
@click.option(
    "--no-checkpoints",
    is_flag=True,
    help="Skip waiting for time checkpoints (wait_for_checkpoint steps return immediately)",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode: breakpoint() steps pause and wait for a button press (otherwise no-op).",
)
@click.option(
    "--record-localization",
    is_flag=True,
    help="Record particle-filter state to .raccoon/runs/<run_id>/localization.jsonl (off by default).",
)
@click.option(
    "--no-record",
    is_flag=True,
    help="Force-disable localization recording (redundant now that recording is off by default; kept for compatibility).",
)
@click.option(
    "--no-profile",
    is_flag=True,
    help="Disable step profiling for this run (profiling is on by default).",
)
@click.option(
    "--record-hz",
    type=float,
    default=None,
    help="Localization recorder downsample rate in Hz (default 20).",
)
@click.option(
    "--record-sensors/--no-record-sensors",
    default=True,
    help="Record raw SHM sensor channels to .raccoon/runs/<run_id>/sensors.mcap "
    "on the Pi for the duration of the run (on by default).",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Local runs: inherit the program's stdout directly instead of the live JSONL log viewer.",
)
@click.pass_context
def run_command(
    ctx: click.Context,
    args: tuple,
    dev: bool,
    local: bool,
    no_sync: bool,
    no_calibrate: bool,
    no_codegen: bool,
    no_checkpoints: bool,
    debug: bool,
    record_localization: bool,
    no_record: bool,
    no_profile: bool,
    record_hz: float | None,
    record_sensors: bool,
    raw: bool,
) -> None:
    """Run codegen and then execute src.main.

    If connected to a Pi, syncs the project and runs remotely.
    Use --local to force local execution.

    The first positional argument may name a run configuration declared
    under ``run_configurations:`` in ``raccoon.project.yml`` (e.g.
    ``raccoon run dev``). The configuration provides defaults for the
    flags below; explicit CLI flags still win.

    Use --no-mN (e.g. --no-m0 --no-m2) to skip missions at those order indices.
    """
    console: Console = ctx.obj["console"]

    # Localization recording is OFF BY DEFAULT; enable it with --record-localization
    # or a run config's `record_localization: true` (localization itself always
    # runs, only the recording/logging is gated). --no-record forces it off and
    # wins over any opt-in — applied after the run-config merge below. Step
    # profiling stays ON BY DEFAULT; --no-profile opts out.
    profile = not no_profile

    # Parse --no-mN flags out of the raw args before forwarding the rest
    args, skip_missions = _extract_skip_missions(args)
    if skip_missions:
        console.print(
            f"[dim]Skipping mission(s) at order: {sorted(skip_missions)}[/dim]"
        )

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

        # Resolve run configuration: first positional arg picks one if it
        # matches a known name. CLI flags then layer on top of the config.
        run_configs = load_run_configurations(project_root, config)
        args, run_cfg = _extract_run_config(args, run_configs)
        extra_env: dict[str, str] = {}
        if run_cfg is not None:
            console.print(
                f"[cyan]Run configuration:[/cyan] {run_cfg.name}"
                + (f" — {run_cfg.description}" if run_cfg.description else "")
            )
            dev = dev or run_cfg.dev
            no_calibrate = no_calibrate or run_cfg.no_calibrate
            no_checkpoints = no_checkpoints or run_cfg.no_checkpoints
            debug = debug or run_cfg.debug
            no_codegen = no_codegen or run_cfg.no_codegen
            no_sync = no_sync or run_cfg.no_sync
            # A run config can enable recording (opt-in, off by default) or opt
            # out of profiling (on by default). --no-record still wins, enforced
            # after this block.
            record_localization = record_localization or run_cfg.record_localization
            profile = profile and run_cfg.profile
            # Sensor recording is ON by default; a run config may opt out with
            # `record_sensors: false`. --no-record-sensors on the CLI also wins.
            record_sensors = record_sensors and getattr(run_cfg, "record_sensors", True)
            if record_hz is None and run_cfg.record_hz is not None:
                record_hz = run_cfg.record_hz
            if run_cfg.target == "local":
                local = True
            elif run_cfg.target == "remote":
                local = False
            if run_cfg.args:
                args = tuple(run_cfg.args) + args
            extra_env = dict(run_cfg.env)

        # --no-record wins over any opt-in (CLI --record-localization or run config).
        if no_record:
            record_localization = False

        _warn_if_migrations_pending(console, project_root)

        # One run_id for this invocation — names the unified artifact dir
        # .raccoon/runs/<run_id>/ for the log, localization, profile, manifest.
        run_id = make_run_id()

        # Check if we should run remotely
        if not local:
            from raccoon_cli.client.connection import (
                get_connection_manager,
                ParamikoVersionError,
                print_paramiko_version_error,
            )

            manager = get_connection_manager()

            # Try to auto-connect from project or global config if not connected
            if not manager.is_connected:
                try:
                    # Try project config first
                    project_config = manager.load_from_project(project_root)
                    if project_config and project_config.pi_address:
                        logger.info(
                            f"Connecting to Pi from project config: {project_config.pi_address}"
                        )
                        manager.connect_sync(
                            project_config.pi_address,
                            project_config.pi_port,
                            project_config.pi_user,
                        )
                    else:
                        # Try global config
                        known_pis = manager.load_known_pis()
                        if known_pis:
                            pi = known_pis[0]
                            logger.info(f"Connecting to known Pi: {pi.get('address')}")
                            manager.connect_sync(
                                pi.get("address"), pi.get("port", 8421)
                            )
                except ParamikoVersionError as e:
                    print_paramiko_version_error(e, console)
                    raise SystemExit(1)
                except Exception as e:
                    console.print(f"[red]Failed to connect to Pi: {e}[/red]")
                    raise SystemExit(1)

            if manager.is_connected:
                # Run remotely
                asyncio.run(
                    _run_remote(
                        ctx,
                        project_root,
                        config,
                        args,
                        dev=dev,
                        no_calibrate=no_calibrate,
                        no_codegen=no_codegen,
                        no_sync=no_sync,
                        no_checkpoints=no_checkpoints,
                        debug=debug,
                        skip_missions=skip_missions,
                        record_localization=record_localization,
                        profile=profile,
                        record_hz=record_hz,
                        record_sensors=record_sensors,
                        extra_env=extra_env,
                        run_id=run_id,
                    )
                )
                return

            console.print(
                "[red]Remote execution requested, but no Pi connection is available.[/red]"
            )
            console.print(
                "Run [cyan]raccoon connect <PI_ADDRESS>[/cyan] or use [cyan]--local[/cyan]."
            )
            raise SystemExit(1)

        # Run locally
        _run_local(
            ctx,
            project_root,
            config,
            args,
            dev=dev,
            no_calibrate=no_calibrate,
            no_codegen=no_codegen,
            no_checkpoints=no_checkpoints,
            debug=debug,
            skip_missions=skip_missions,
            record_localization=record_localization,
            profile=profile,
            record_hz=record_hz,
            record_sensors=record_sensors,
            extra_env=extra_env,
            raw=raw,
            run_id=run_id,
        )

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error while running project")
        raise SystemExit(1) from None
