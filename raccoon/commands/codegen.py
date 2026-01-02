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
) -> None:
    """Run code generation locally."""
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = project_root / "src" / "hardware"

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
) -> None:
    """Run code generation on the connected Pi and sync back."""
    from raccoon.client.connection import get_connection_manager
    from raccoon.client.api import create_api_client
    from raccoon.client.output_handler import OutputHandler
    from raccoon.commands.sync_cmd import sync_project_to_pi
    from raccoon.client.sftp_sync import SftpSync

    manager = get_connection_manager()
    state = manager.state
    project_uuid = config.get("uuid")
    project_name = config.get("name", project_root.name)

    console.print(f"[cyan]Running codegen for '{project_name}' on {state.pi_hostname}...[/cyan]")

    # Step 1: Sync project to Pi
    console.print("[dim]Syncing project to Pi...[/dim]")
    if not sync_project_to_pi(project_root, console):
        console.print("[red]Failed to sync project to Pi[/red]")
        raise SystemExit(1)
    console.print("[dim]Sync complete[/dim]")
    console.print()

    # Step 2: Build args for remote codegen
    args = []
    if only:
        for o in only:
            args.extend(["--only", o])
    if no_format:
        args.append("--no-format")
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

        if exit_code != 0:
            console.print()
            console.print(f"[red]Codegen failed with exit code {exit_code}[/red]")
            raise SystemExit(exit_code)

    # Step 4: Sync generated files back from Pi
    console.print()
    console.print("[cyan]Syncing generated files back from Pi...[/cyan]")

    try:
        ssh_client = manager.get_ssh_client()
        remote_path = f"/home/{state.pi_user}/programs/{project_uuid}"

        # Use SFTP to pull the generated files
        sftp = ssh_client.open_sftp()

        # Determine which files to sync back
        local_hardware_dir = project_root / "src" / "hardware"
        remote_hardware_dir = f"{remote_path}/src/hardware"

        local_hardware_dir.mkdir(parents=True, exist_ok=True)

        # List and download generated files
        try:
            remote_files = sftp.listdir(remote_hardware_dir)
            for filename in remote_files:
                if filename.endswith(".py"):
                    remote_file = f"{remote_hardware_dir}/{filename}"
                    local_file = local_hardware_dir / filename
                    sftp.get(remote_file, str(local_file))
                    console.print(f"  [dim]← {filename}[/dim]")
        except FileNotFoundError:
            console.print("[yellow]No generated files found on Pi[/yellow]")

        sftp.close()
        console.print("[green]Sync complete![/green]")

    except Exception as e:
        console.print(f"[yellow]Warning: Could not sync files back: {e}[/yellow]")
        console.print("[dim]Generated files are on the Pi but not synced locally.[/dim]")


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
@click.pass_context
def codegen_command(
    ctx: click.Context,
    only: tuple,
    no_format: bool,
    output_dir: str | None,
    local: bool,
    no_sync: bool,
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
            from raccoon.client.connection import get_connection_manager

            manager = get_connection_manager()

            # Try to auto-connect from project or global config if not connected
            if not manager.is_connected:
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

            if manager.is_connected:
                # Run remotely
                asyncio.run(_codegen_remote(console, project_root, config, only, no_format, output_dir))
                return

        # Run locally
        _codegen_local(console, project_root, config, only, no_format, output_dir)

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error during code generation")
        raise SystemExit(1) from None
