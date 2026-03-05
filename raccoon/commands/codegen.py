"""Codegen command for raccoon CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from raccoon.codegen import create_pipeline
from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


def _render_codegen_success(
    console: Console,
    results: Dict[str, Path],
    project_root: Path,
    output_dir: Path,
    formatted: bool,
    filtered: bool,
) -> None:
    """Display a concise overlay of generated files."""
    heading = Text(
        "Code generation complete",
        style="bold green",
    )

    subtitle_parts = []
    try:
        output_display = output_dir.relative_to(project_root)
    except ValueError:
        output_display = output_dir
    subtitle_parts.append(f"Output: {output_display}")
    subtitle_parts.append("Formatting: on" if formatted else "Formatting: off")
    if filtered:
        subtitle_parts.append("Mode: filtered")

    subtitle = Text(" | ".join(subtitle_parts), style="dim")

    table = Table(
        "Generator",
        "File",
        box=box.MINIMAL_DOUBLE_HEAD,
        header_style="bold cyan",
        expand=True,
    )

    for name, path in results.items():
        try:
            display_path = path.relative_to(project_root)
        except ValueError:
            display_path = path
        table.add_row(name, str(display_path))

    console.print(
        Panel(
            table,
            title=heading,
            subtitle=subtitle,
            border_style="cyan",
            padding=(1, 2),
        )
    )


def _codegen_local(
    console: Console,
    project_root: Path,
    config: dict,
    only: tuple,
    no_format: bool,
    output_dir: str | None,
    force: bool = False,
) -> None:
    """Run code generation locally."""
    import sys

    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = project_root / "src" / "hardware"

    # Add project root to sys.path so user-defined types (e.g.
    # src.hardware.thresholded_sensor.ThresholdedSensor) can be resolved.
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

    # Clear cache if --force flag is set
    if force:
        from raccoon.codegen.cache import CodegenCache
        cache = CodegenCache(out_dir)
        cache.clear()
        logger.info("Cache cleared (--force)")

    pipeline = create_pipeline()

    format_code = not no_format
    filtered = bool(only)
    if filtered:
        results = pipeline.run_specific(list(only), config, out_dir, format_code)
    else:
        results = pipeline.run_all(config, out_dir, format_code)

    _render_codegen_success(
        console,
        results,
        project_root,
        out_dir,
        formatted=format_code,
        filtered=filtered,
    )


async def _codegen_remote(
    console: Console,
    project_root: Path,
    config: dict,
    only: tuple,
    no_format: bool,
    output_dir: str | None,
    force: bool = False,
) -> None:
    """Run code generation on the connected Pi and sync back."""
    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.client.sftp_sync import SyncDirection
    from raccoon.commands.sync_cmd import sync_project_interactive

    # Sync project to Pi before codegen
    if not sync_project_interactive(project_root, console):
        console.print("[red]Sync failed, cannot run codegen remotely[/red]")
        raise SystemExit(1)
    console.print()

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running codegen for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Step 2: Build args for remote codegen
    args = []
    if only:
        for o in only:
            args.extend(["--only", o])
    if no_format:
        args.append("--no-format")
    if force:
        args.append("--force")
    if output_dir:
        args.extend(["--output-dir", output_dir])

    # Step 3: Run codegen on Pi
    async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
        try:
            result = await client.codegen_project(project_uuid, args=args)
        except Exception as e:
            console.print(f"[red]Failed to start codegen on Pi: {e}[/red]")
            raise SystemExit(1)

        # Stream output via WebSocket
        ws_url = client.get_websocket_url(result.command_id)
        handler = OutputHandler(ws_url)

        console.print(f"[dim]Command ID: {result.command_id}[/dim]")
        console.print()

        final_status = handler.stream_to_console(console)

        exit_code = final_status.get("exit_code", -1)

    # Sync generated files back from Pi (do this even if exit code non-zero,
    # since codegen may have succeeded but cleanup crashed)
    console.print()
    console.print("[cyan]Syncing generated files from Pi...[/cyan]")
    sync_failed = not sync_project_interactive(project_root, console, direction=SyncDirection.PULL)
    if sync_failed:
        console.print("[yellow]Warning: Could not sync files back[/yellow]")

    # Now check exit code after sync attempt
    if exit_code != 0:
        if exit_code == -11:
            # SIGSEGV in cleanup - codegen likely succeeded but shutdown crashed
            console.print()
            console.print(f"[yellow]Warning: Process crashed during cleanup (exit code {exit_code})[/yellow]")
            if not sync_failed:
                console.print("[dim]Files were synced successfully despite the crash.[/dim]")
        else:
            console.print()
            console.print(f"[red]Codegen failed with exit code {exit_code}[/red]")
            raise SystemExit(exit_code)


@click.command(name="codegen")
@click.option(
    "--only",
    multiple=True,
    help="Generate specific file(s): defs, robot. May be given multiple times.",
)
@click.option("--no-format", is_flag=True, help="Skip black formatting")
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(),
    default=None,
    help="Override output directory (default: src/hardware/)",
)
@click.option("--local", "-l", is_flag=True, help="Force local execution (skip remote)")
@click.option("--no-sync", is_flag=True, help="Skip syncing (for internal use on Pi)")
@click.option("--force", "-f", is_flag=True, help="Force regeneration, ignoring cache")
@click.pass_context
def codegen_command(
    ctx: click.Context,
    only: tuple,
    no_format: bool,
    output_dir: str | None,
    local: bool,
    no_sync: bool,
    force: bool,
) -> None:
    """Generate Python code from raccoon.project.yml.

    If connected to a Pi, runs codegen remotely and syncs the generated
    files back to your local machine.

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
        if not local and not no_sync:
            from raccoon.client.connection import (
                get_connection_manager,
                ParamikoVersionError,
                print_paramiko_version_error,
            )

            manager = get_connection_manager()

            # Try to auto-connect from project or global config if not connected
            if not manager.is_connected:
                try:
                    project_config = manager.load_from_project(project_root)
                    if project_config and project_config.pi_address:
                        logger.info(f"Connecting to Pi from project config: {project_config.pi_address}")
                        manager.connect_sync(project_config.pi_address, project_config.pi_port, project_config.pi_user)
                    else:
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
                asyncio.run(_codegen_remote(console, project_root, config, only, no_format, output_dir, force))
                return

            console.print("[red]Remote execution requested, but no Pi connection is available.[/red]")
            console.print("Run [cyan]raccoon connect <PI_ADDRESS>[/cyan] or use [cyan]--local[/cyan].")
            raise SystemExit(1)

        # Run locally
        _codegen_local(console, project_root, config, only, no_format, output_dir, force)

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error during code generation")
        raise SystemExit(1) from None
