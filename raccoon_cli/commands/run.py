"""Run command for raccoon CLI."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import os
import subprocess
import sys
import threading
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from raccoon_cli.checkpoint import create_checkpoint
from raccoon_cli.codegen import create_pipeline
from raccoon_cli.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")

_NO_MISSION_RE = re.compile(r"^--no-m(\d+)$")


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


_WARN_ERROR_RE = re.compile(r"\b(WARNING|WARN|ERROR|CRITICAL|FATAL)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _is_warn_or_error(line: str) -> bool:
    return bool(_WARN_ERROR_RE.search(_ANSI_RE.sub("", line)))


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


def _run_local(
    ctx: click.Context, project_root: Path, config: dict, args: tuple,
    dev: bool = False, no_calibrate: bool = False, no_codegen: bool = False,
    no_checkpoints: bool = False, skip_missions: set[int] | None = None,
) -> None:
    """Run the project locally."""
    console: Console = ctx.obj["console"]

    if config.get("auto_checkpoints", True):
        result = create_checkpoint(project_root, label="pre-run")
        if result.created:
            console.print(f"[dim]Checkpoint {result.short_sha} saved[/dim]")

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if not no_codegen:
        pipeline = create_pipeline()
        output_dir = project_root / "src" / "hardware"
        pipeline.run_all(config, output_dir, format_code=True)

    logger.info("Running src.main...")
    cmd_parts = [sys.executable, "-m", "src.main", *args]
    logger.info(f"Executing: {' '.join(cmd_parts)}")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if dev:
        env["LIBSTP_DEV_MODE"] = "1"
    if no_calibrate:
        env["LIBSTP_NO_CALIBRATE"] = "1"
    if no_checkpoints:
        env["LIBSTP_NO_CHECKPOINTS"] = "1"
    if skip_missions:
        env["LIBSTP_SKIP_MISSIONS"] = ",".join(str(i) for i in sorted(skip_missions))

    collected: list[str] = []

    # On Windows, Ctrl+C doesn't reliably propagate to child processes.
    # Use Popen so we can catch SIGINT ourselves and terminate the child.
    # Pipe stdout+stderr so we can collect warnings/errors for the summary.
    proc = subprocess.Popen(
        cmd_parts, cwd=project_root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True,
    )

    def _stream_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(line)
            if _is_warn_or_error(line):
                collected.append(line)

    reader = threading.Thread(target=_stream_output, daemon=True)
    reader.start()

    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl+C — stopping program...[/yellow]")
        proc.terminate()
        try:
            returncode = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = proc.wait()

    reader.join()

    _print_output_summary(console, collected)

    exit_style = "bold green" if returncode == 0 else "bold red"
    console.print(
        Panel.fit(
            Text(f"src.main exited with code {returncode}", style=exit_style),
            border_style="green" if returncode == 0 else "red",
        )
    )

    if returncode != 0:
        raise SystemExit(returncode)


async def _run_remote(
    ctx: click.Context, project_root: Path, config: dict, args: tuple,
    dev: bool = False, no_calibrate: bool = False,
    no_checkpoints: bool = False, skip_missions: set[int] | None = None,
) -> None:
    """Run the project on the connected Pi."""
    console: Console = ctx.obj["console"]

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
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    pipeline = create_pipeline()
    output_dir = project_root / "src" / "hardware"
    pipeline.run_all(config, output_dir, format_code=True)

    # Sync project to Pi before running
    if not sync_project_interactive(project_root, console):
        console.print("[red]Sync failed, cannot run remotely[/red]")
        raise SystemExit(1)
    console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Start the run command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            env = {}
            if dev:
                env["LIBSTP_DEV_MODE"] = "1"
            if no_calibrate:
                env["LIBSTP_NO_CALIBRATE"] = "1"
            if no_checkpoints:
                env["LIBSTP_NO_CHECKPOINTS"] = "1"
            if skip_missions:
                env["LIBSTP_SKIP_MISSIONS"] = ",".join(str(i) for i in sorted(skip_missions))
            result = await client.run_project(project_uuid, args=list(args), env=env)
        except Exception as e:
            console.print(f"[red]Failed to start run on Pi: {e}[/red]")
            raise SystemExit(1)

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
        sync_project_interactive(project_root, console, direction=SyncDirection.PULL, update=True)

        # Display final status
        exit_code = final_status.get("exit_code", -1)
        status = final_status.get("status", "unknown")

        exit_style = "bold green" if exit_code == 0 else "bold red"
        console.print()
        console.print(
            Panel.fit(
                Text(f"Remote execution {status} with code {exit_code}", style=exit_style),
                border_style="green" if exit_code == 0 else "red",
            )
        )

        if exit_code != 0:
            raise SystemExit(exit_code)


@click.command(name="run", context_settings=dict(allow_extra_args=True, ignore_unknown_options=True))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--dev", is_flag=True, help="Dev mode: use button instead of wait-for-light")
@click.option("--local", "-l", is_flag=True, help="Force local execution (skip remote)")
@click.option("--no-sync", is_flag=True, help="Skip syncing before remote run")
@click.option("--no-calibrate", is_flag=True, help="Skip calibration steps, use stored values")
@click.option("--no-codegen", is_flag=True, help="Skip code generation (used by server when codegen was done client-side)")
@click.option("--no-checkpoints", is_flag=True, help="Skip waiting for time checkpoints (wait_for_checkpoint steps return immediately)")
@click.pass_context
def run_command(ctx: click.Context, args: tuple, dev: bool, local: bool, no_sync: bool, no_calibrate: bool, no_codegen: bool, no_checkpoints: bool) -> None:
    """Run codegen and then execute src.main.

    If connected to a Pi, syncs the project and runs remotely.
    Use --local to force local execution.

    Use --no-mN (e.g. --no-m0 --no-m2) to skip missions at those order indices.
    """
    console: Console = ctx.obj["console"]

    # Parse --no-mN flags out of the raw args before forwarding the rest
    args, skip_missions = _extract_skip_missions(args)
    if skip_missions:
        console.print(f"[dim]Skipping mission(s) at order: {sorted(skip_missions)}[/dim]")

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

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
                        logger.info(f"Connecting to Pi from project config: {project_config.pi_address}")
                        manager.connect_sync(project_config.pi_address, project_config.pi_port, project_config.pi_user)
                    else:
                        # Try global config
                        known_pis = manager.load_known_pis()
                        if known_pis:
                            pi = known_pis[0]
                            logger.info(f"Connecting to known Pi: {pi.get('address')}")
                            manager.connect_sync(pi.get("address"), pi.get("port", 8421))
                except ParamikoVersionError as e:
                    print_paramiko_version_error(e, console)
                    raise SystemExit(1)
                except Exception as e:
                    console.print(f"[red]Failed to connect to Pi: {e}[/red]")
                    raise SystemExit(1)

            if manager.is_connected:
                # Run remotely
                asyncio.run(_run_remote(ctx, project_root, config, args, dev=dev, no_calibrate=no_calibrate, no_checkpoints=no_checkpoints, skip_missions=skip_missions))
                return

            console.print("[red]Remote execution requested, but no Pi connection is available.[/red]")
            console.print("Run [cyan]raccoon connect <PI_ADDRESS>[/cyan] or use [cyan]--local[/cyan].")
            raise SystemExit(1)

        # Run locally
        _run_local(ctx, project_root, config, args, dev=dev, no_calibrate=no_calibrate, no_codegen=no_codegen, no_checkpoints=no_checkpoints, skip_missions=skip_missions)

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error while running project")
        raise SystemExit(1) from None
