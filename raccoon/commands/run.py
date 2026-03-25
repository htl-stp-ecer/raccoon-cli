"""Run command for raccoon CLI."""

from __future__ import annotations

import asyncio
import logging
import signal
import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from raccoon.checkpoint import create_checkpoint
from raccoon.codegen import create_pipeline
from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


def _run_local(
    ctx: click.Context, project_root: Path, config: dict, args: tuple,
    dev: bool = False, no_calibrate: bool = False, no_codegen: bool = False,
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
    if dev:
        env["LIBSTP_DEV_MODE"] = "1"
    if no_calibrate:
        env["LIBSTP_NO_CALIBRATE"] = "1"

    # On Windows, Ctrl+C doesn't reliably propagate to child processes.
    # Use Popen so we can catch SIGINT ourselves and terminate the child.
    proc = subprocess.Popen(cmd_parts, cwd=project_root, env=env)
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
) -> None:
    """Run the project on the connected Pi."""
    console: Console = ctx.obj["console"]

    if config.get("auto_checkpoints", True):
        result = create_checkpoint(project_root, label="pre-run")
        if result.created:
            console.print(f"[dim]Checkpoint {result.short_sha} saved[/dim]")

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.client.sftp_sync import SyncDirection
    from raccoon.commands.sync_cmd import sync_project_interactive

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

        try:
            final_status = handler.stream_to_console(console)
        finally:
            signal.signal(signal.SIGINT, original_handler)

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


@click.command(name="run")
@click.argument("args", nargs=-1)
@click.option("--dev", is_flag=True, help="Dev mode: use button instead of wait-for-light")
@click.option("--local", "-l", is_flag=True, help="Force local execution (skip remote)")
@click.option("--no-sync", is_flag=True, help="Skip syncing before remote run")
@click.option("--no-calibrate", is_flag=True, help="Skip calibration steps, use stored values")
@click.option("--no-codegen", is_flag=True, help="Skip code generation (used by server when codegen was done client-side)")
@click.pass_context
def run_command(ctx: click.Context, args: tuple, dev: bool, local: bool, no_sync: bool, no_calibrate: bool, no_codegen: bool) -> None:
    """Run codegen and then execute src.main.

    If connected to a Pi, syncs the project and runs remotely.
    Use --local to force local execution.
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

        # Check if we should run remotely
        if not local:
            from raccoon.client.connection import (
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
                asyncio.run(_run_remote(ctx, project_root, config, args, dev=dev, no_calibrate=no_calibrate))
                return

            console.print("[red]Remote execution requested, but no Pi connection is available.[/red]")
            console.print("Run [cyan]raccoon connect <PI_ADDRESS>[/cyan] or use [cyan]--local[/cyan].")
            raise SystemExit(1)

        # Run locally
        _run_local(ctx, project_root, config, args, dev=dev, no_calibrate=no_calibrate, no_codegen=no_codegen)

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error while running project")
        raise SystemExit(1) from None
