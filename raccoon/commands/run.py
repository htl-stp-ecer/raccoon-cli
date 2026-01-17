"""Run command for raccoon CLI."""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from raccoon.codegen import create_pipeline
from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


def _run_local(ctx: click.Context, project_root: Path, config: dict, args: tuple) -> None:
    """Run the project locally."""
    console: Console = ctx.obj["console"]

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    pipeline = create_pipeline()
    output_dir = project_root / "src" / "hardware"
    pipeline.run_all(config, output_dir, format_code=True)

    logger.info("Running src.main...")
    cmd_parts = [sys.executable, "-m", "src.main", *args]
    logger.info(f"Executing: {' '.join(cmd_parts)}")

    result = subprocess.run(cmd_parts, cwd=project_root)

    exit_style = "bold green" if result.returncode == 0 else "bold red"
    console.print(
        Panel.fit(
            Text(f"src.main exited with code {result.returncode}", style=exit_style),
            border_style="green" if result.returncode == 0 else "red",
        )
    )

    if result.returncode != 0:
        raise SystemExit(result.returncode)


async def _run_remote(ctx: click.Context, project_root: Path, config: dict, args: tuple) -> None:
    """Run the project on the connected Pi."""
    console: Console = ctx.obj["console"]

    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.commands.sync_cmd import sync_project_to_pi

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Sync project first
    console.print("[dim]Syncing project...[/dim]")
    if not sync_project_to_pi(project_root, console):
        console.print("[red]Failed to sync project to Pi[/red]")
        raise SystemExit(1)
    console.print("[dim]Sync complete[/dim]")
    console.print()

    # Start the run command on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.run_project(project_uuid, args=list(args))
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
@click.option("--local", "-l", is_flag=True, help="Force local execution (skip remote)")
@click.option("--no-sync", is_flag=True, help="Skip syncing before remote run")
@click.pass_context
def run_command(ctx: click.Context, args: tuple, local: bool, no_sync: bool) -> None:
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
            from raccoon.client.connection import get_connection_manager

            manager = get_connection_manager()

            # Try to auto-connect from project or global config if not connected
            if not manager.is_connected:
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

            if manager.is_connected:
                # Run remotely
                asyncio.run(_run_remote(ctx, project_root, config, args))
                return

        # Run locally
        _run_local(ctx, project_root, config, args)

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error while running project")
        raise SystemExit(1) from None
