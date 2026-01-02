"""LCM spy and debug commands for raccoon CLI."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from raccoon.client.connection import get_connection_manager
from raccoon.client.api import create_api_client
from raccoon.project import find_project_root

logger = logging.getLogger("raccoon")


def _ensure_connected(console: Console) -> bool:
    """
    Ensure we're connected to a Pi, auto-connecting from saved config if needed.

    Returns True if connected, False otherwise.
    """
    manager = get_connection_manager()

    if manager.is_connected:
        return True

    # Try to auto-connect from project config first
    project_root = find_project_root()
    if project_root:
        project_config = manager.load_from_project(project_root)
        if project_config and project_config.pi_address:
            logger.info(f"Connecting to Pi from project config: {project_config.pi_address}")
            console.print(f"[dim]Connecting to {project_config.pi_address}...[/dim]")
            if manager.connect_sync(
                project_config.pi_address,
                project_config.pi_port,
                project_config.pi_user,
            ):
                return True

    # Try global config
    known_pis = manager.load_known_pis()
    if known_pis:
        pi = known_pis[0]
        logger.info(f"Connecting to known Pi: {pi.get('address')}")
        console.print(f"[dim]Connecting to {pi.get('address')}...[/dim]")
        if manager.connect_sync(pi.get("address"), pi.get("port", 8421)):
            return True

    return False


@click.group(name="lcm")
@click.pass_context
def lcm_group(ctx: click.Context) -> None:
    """LCM spy and debugging commands.

    Spy on LCM traffic, record sessions, and playback recordings.
    """
    pass


@lcm_group.command(name="spy")
@click.option(
    "--channel",
    "-c",
    multiple=True,
    help="Channel pattern to filter (can specify multiple, supports wildcards)",
)
@click.option("--record", "-r", type=str, help="Record to file (filename)")
@click.option(
    "--format",
    "-f",
    type=click.Choice(["table", "json", "compact"]),
    default="table",
    help="Output format",
)
@click.pass_context
def spy_command(
    ctx: click.Context,
    channel: tuple,
    record: Optional[str],
    format: str,
) -> None:
    """Spy on LCM traffic in real-time.

    By default, shows all channels. Use --channel to filter.

    Examples:

        raccoon lcm spy

        raccoon lcm spy --channel "SENSOR_*"

        raccoon lcm spy --channel "MOTOR_CMD" --channel "SENSOR_DATA"

        raccoon lcm spy --record my_session
    """
    console: Console = ctx.obj.get("console", Console())

    if not _ensure_connected(console):
        console.print("[red]Not connected to a Pi. Run 'raccoon connect' first.[/red]")
        raise SystemExit(1)

    manager = get_connection_manager()
    asyncio.run(_run_spy(console, manager, list(channel), record, format))


async def _run_spy(
    console: Console,
    manager,
    channels: list[str],
    record: Optional[str],
    format: str,
) -> None:
    """Run the spy session."""
    from raccoon.client.lcm_handler import LcmOutputHandler

    state = manager.state

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        # Start spy on server
        try:
            result = await client.start_lcm_spy(
                channel_patterns=channels or None,
                record_to=record,
            )
        except Exception as e:
            console.print(f"[red]Failed to start LCM spy: {e}[/red]")
            return

        console.print("[green]LCM Spy started[/green]")
        if channels:
            console.print(f"  Channels: {', '.join(channels)}")
        else:
            console.print("  Channels: [dim]all[/dim]")
        if record:
            console.print(f"  Recording to: {result.get('recording_file', record)}")
        console.print()
        console.print("[dim]Press Ctrl+C to stop[/dim]")
        console.print()

        # Connect to WebSocket for live stream
        ws_url = client.get_lcm_websocket_url()
        handler = LcmOutputHandler(ws_url, format=format)

        # Handle Ctrl+C
        stop_requested = False

        def signal_handler(sig, frame):
            nonlocal stop_requested
            stop_requested = True

        original_handler = signal.signal(signal.SIGINT, signal_handler)

        try:
            await handler.stream_to_console_async(
                console,
                stop_check=lambda: stop_requested,
            )
        finally:
            signal.signal(signal.SIGINT, original_handler)

        # Stop spy on server
        try:
            stop_result = await client.stop_lcm_spy()
            console.print()
            console.print("[yellow]LCM Spy stopped[/yellow]")
            console.print(f"  Messages captured: {stop_result.get('message_count', 0)}")
            channels_seen = stop_result.get("channels_seen", [])
            if channels_seen:
                console.print(f"  Channels seen: {', '.join(channels_seen)}")
            if stop_result.get("recording_file"):
                console.print(f"  Saved to: {stop_result['recording_file']}")
        except Exception:
            pass


@lcm_group.command(name="record")
@click.argument("filename")
@click.option(
    "--channel",
    "-c",
    multiple=True,
    help="Channel pattern to filter (supports wildcards)",
)
@click.option(
    "--duration",
    "-d",
    type=int,
    default=0,
    help="Recording duration in seconds (0 = until stopped)",
)
@click.pass_context
def record_command(
    ctx: click.Context,
    filename: str,
    channel: tuple,
    duration: int,
) -> None:
    """Record LCM traffic to a file on the Pi.

    Records are stored on the Pi and can be listed with 'raccoon lcm list'.

    Examples:

        raccoon lcm record my_session

        raccoon lcm record test_run --channel "SENSOR_*" --duration 60
    """
    console: Console = ctx.obj.get("console", Console())

    if not _ensure_connected(console):
        console.print("[red]Not connected to a Pi. Run 'raccoon connect' first.[/red]")
        raise SystemExit(1)

    manager = get_connection_manager()
    asyncio.run(_run_record(console, manager, filename, list(channel), duration))


async def _run_record(
    console: Console,
    manager,
    filename: str,
    channels: list[str],
    duration: int,
) -> None:
    """Run recording session (no live display)."""
    state = manager.state

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            result = await client.start_lcm_spy(
                channel_patterns=channels or None,
                record_to=filename,
            )
        except Exception as e:
            console.print(f"[red]Failed to start recording: {e}[/red]")
            return

        console.print(
            f"[green]Recording started: {result.get('recording_file', filename)}[/green]"
        )
        if channels:
            console.print(f"  Channels: {', '.join(channels)}")

        # Handle Ctrl+C
        stop_requested = False

        def signal_handler(sig, frame):
            nonlocal stop_requested
            stop_requested = True

        original_handler = signal.signal(signal.SIGINT, signal_handler)

        try:
            if duration > 0:
                console.print(f"Recording for {duration} seconds...")
                with console.status(f"Recording... (0/{duration}s)") as status:
                    for i in range(duration):
                        if stop_requested:
                            break
                        await asyncio.sleep(1)
                        # Check status
                        try:
                            spy_status = await client.get_lcm_spy_status()
                            msg_count = spy_status.get("message_count", 0)
                            status.update(f"Recording... ({i+1}/{duration}s) - {msg_count} messages")
                        except Exception:
                            status.update(f"Recording... ({i+1}/{duration}s)")
            else:
                console.print("[dim]Press Ctrl+C to stop recording[/dim]")
                while not stop_requested:
                    await asyncio.sleep(0.5)
                    try:
                        spy_status = await client.get_lcm_spy_status()
                        if spy_status.get("status") != "running":
                            break
                    except Exception:
                        pass
        finally:
            signal.signal(signal.SIGINT, original_handler)

        stop_result = await client.stop_lcm_spy()
        console.print()
        console.print("[green]Recording complete[/green]")
        console.print(f"  Messages: {stop_result.get('message_count', 0)}")
        console.print(f"  File: {stop_result.get('recording_file', filename)}")


@lcm_group.command(name="list")
@click.pass_context
def list_command(ctx: click.Context) -> None:
    """List available LCM recordings on the Pi."""
    console: Console = ctx.obj.get("console", Console())

    if not _ensure_connected(console):
        console.print("[red]Not connected to a Pi. Run 'raccoon connect' first.[/red]")
        raise SystemExit(1)

    manager = get_connection_manager()
    asyncio.run(_list_recordings(console, manager))


async def _list_recordings(console: Console, manager) -> None:
    """List recordings from Pi."""
    state = manager.state

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            recordings = await client.list_lcm_recordings()
        except Exception as e:
            console.print(f"[red]Failed to list recordings: {e}[/red]")
            return

        if not recordings:
            console.print("[dim]No recordings found[/dim]")
            return

        table = Table(title="LCM Recordings")
        table.add_column("Filename", style="cyan")
        table.add_column("Messages", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Created")

        for rec in recordings:
            size = _format_size(rec["size_bytes"])
            created = rec["created_at"][:19].replace("T", " ")
            table.add_row(
                rec["filename"],
                str(rec["message_count"]),
                size,
                created,
            )

        console.print(table)


@lcm_group.command(name="playback")
@click.argument("filename")
@click.option("--speed", "-s", type=float, default=1.0, help="Playback speed multiplier")
@click.option("--loop", "-l", is_flag=True, help="Loop playback")
@click.option(
    "--channel",
    "-c",
    multiple=True,
    help="Channel pattern to filter (supports wildcards)",
)
@click.pass_context
def playback_command(
    ctx: click.Context,
    filename: str,
    speed: float,
    loop: bool,
    channel: tuple,
) -> None:
    """Playback a recorded LCM session.

    Republishes recorded messages to LCM on the Pi.

    Examples:

        raccoon lcm playback my_session.jsonl

        raccoon lcm playback test_run.jsonl --speed 2.0

        raccoon lcm playback demo.jsonl --loop
    """
    console: Console = ctx.obj.get("console", Console())

    if not _ensure_connected(console):
        console.print("[red]Not connected to a Pi. Run 'raccoon connect' first.[/red]")
        raise SystemExit(1)

    manager = get_connection_manager()
    asyncio.run(_run_playback(console, manager, filename, speed, loop, list(channel)))


async def _run_playback(
    console: Console,
    manager,
    filename: str,
    speed: float,
    loop: bool,
    channels: list[str],
) -> None:
    """Run playback session."""
    state = manager.state

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        # First check if the file exists
        recordings = await client.list_lcm_recordings()
        available_files = [r["filename"] for r in recordings]

        # Add .jsonl extension if not present
        check_filename = filename if filename.endswith(".jsonl") else f"{filename}.jsonl"

        if check_filename not in available_files and filename not in available_files:
            console.print(f"[red]Recording not found: {filename}[/red]")
            if available_files:
                console.print(f"[dim]Available recordings: {', '.join(available_files)}[/dim]")
            else:
                console.print("[dim]No recordings available. Use 'raccoon lcm record <name>' to create one.[/dim]")
            return

        # Use the correct filename (with or without extension)
        actual_filename = check_filename if check_filename in available_files else filename

        try:
            result = await client.start_lcm_playback(
                filename=actual_filename,
                speed=speed,
                loop=loop,
                channel_filter=channels or None,
            )
        except Exception as e:
            console.print(f"[red]Failed to start playback: {e}[/red]")
            return

        # Stop the data reader service to avoid interference
        service_was_active = False
        try:
            service_status = await client.control_service("stm32_data_reader", "status")
            service_was_active = service_status.get("active", False)
            if service_was_active:
                console.print("[dim]Stopping stm32_data_reader.service...[/dim]")
                await client.control_service("stm32_data_reader", "stop")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not stop data reader service: {e}[/yellow]")

        console.print(f"[green]Playback started: {actual_filename}[/green]")
        console.print(f"  Speed: {speed}x")
        if loop:
            console.print("  [dim]Looping enabled[/dim]")
        if channels:
            console.print(f"  Channels: {', '.join(channels)}")
        console.print()
        console.print("[dim]Press Ctrl+C to stop[/dim]")

        # Handle Ctrl+C
        stop_requested = False

        def signal_handler(sig, frame):
            nonlocal stop_requested
            stop_requested = True

        original_handler = signal.signal(signal.SIGINT, signal_handler)

        try:
            while not stop_requested:
                await asyncio.sleep(0.5)
                status = await client.get_lcm_playback_status()
                if status.get("status") != "running":
                    break
                progress = status.get("messages_played", 0)
                total = status.get("total_messages", 0)
                if total > 0:
                    pct = int(100 * progress / total)
                    console.print(
                        f"\r  Progress: {progress}/{total} ({pct}%)", end=""
                    )
        finally:
            signal.signal(signal.SIGINT, original_handler)

        await client.stop_lcm_playback()
        console.print()
        console.print("[yellow]Playback stopped[/yellow]")

        # Restart the data reader service if it was running before
        if service_was_active:
            try:
                console.print("[dim]Restarting stm32_data_reader.service...[/dim]")
                await client.control_service("stm32_data_reader", "start")
            except Exception as e:
                console.print(f"[yellow]Warning: Could not restart data reader service: {e}[/yellow]")


@lcm_group.command(name="delete")
@click.argument("filename")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def delete_command(ctx: click.Context, filename: str, yes: bool) -> None:
    """Delete an LCM recording from the Pi."""
    console: Console = ctx.obj.get("console", Console())

    if not _ensure_connected(console):
        console.print("[red]Not connected to a Pi. Run 'raccoon connect' first.[/red]")
        raise SystemExit(1)

    if not yes:
        if not click.confirm(f"Delete recording '{filename}'?"):
            return

    manager = get_connection_manager()
    asyncio.run(_delete_recording(console, manager, filename))


async def _delete_recording(console: Console, manager, filename: str) -> None:
    """Delete a recording."""
    state = manager.state

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        # Check available recordings and find the right filename
        recordings = await client.list_lcm_recordings()
        available_files = [r["filename"] for r in recordings]

        # Add .jsonl extension if not present
        check_filename = filename if filename.endswith(".jsonl") else f"{filename}.jsonl"

        if check_filename in available_files:
            actual_filename = check_filename
        elif filename in available_files:
            actual_filename = filename
        else:
            console.print(f"[red]Recording not found: {filename}[/red]")
            if available_files:
                console.print(f"[dim]Available recordings: {', '.join(available_files)}[/dim]")
            else:
                console.print("[dim]No recordings available.[/dim]")
            return

        try:
            await client.delete_lcm_recording(actual_filename)
            console.print(f"[green]Deleted: {actual_filename}[/green]")
        except Exception as e:
            console.print(f"[red]Failed to delete: {e}[/red]")


@lcm_group.command(name="status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Show current LCM spy/playback status."""
    console: Console = ctx.obj.get("console", Console())

    if not _ensure_connected(console):
        console.print("[red]Not connected to a Pi. Run 'raccoon connect' first.[/red]")
        raise SystemExit(1)

    manager = get_connection_manager()
    asyncio.run(_show_status(console, manager))


async def _show_status(console: Console, manager) -> None:
    """Show LCM status."""
    state = manager.state

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            spy_status = await client.get_lcm_spy_status()
            playback_status = await client.get_lcm_playback_status()
            lcm_info = await client.get_lcm_info()
        except Exception as e:
            console.print(f"[red]Failed to get status: {e}[/red]")
            return

        # LCM Info
        lcm_avail = "[green]yes[/green]" if lcm_info.get("lcm_available") else "[red]no[/red]"
        decode_avail = "[green]yes[/green]" if lcm_info.get("decoding_available") else "[yellow]no[/yellow]"
        known_types = lcm_info.get("known_types", [])
        types_str = ", ".join(known_types) if known_types else "[dim]none (install exlcm)[/dim]"

        console.print(
            Panel(
                f"LCM Available: {lcm_avail}\n"
                f"Decoding: {decode_avail}\n"
                f"Known Types: {types_str}\n"
                f"Recordings Dir: {lcm_info.get('recordings_dir', 'unknown')}",
                title="LCM Capabilities",
            )
        )

        # Spy status
        spy_running = spy_status.get("status") == "running"
        spy_color = "green" if spy_running else "dim"

        console.print(
            Panel(
                f"Status: [{spy_color}]{spy_status.get('status', 'unknown')}[/{spy_color}]\n"
                f"Messages: {spy_status.get('message_count', 0)}\n"
                f"Channels: {', '.join(spy_status.get('channels_seen', [])) or 'none'}\n"
                f"Recording: {spy_status.get('recording_file') or 'none'}",
                title="LCM Spy",
            )
        )

        # Playback status
        pb_running = playback_status.get("status") == "running"
        pb_color = "green" if pb_running else "dim"

        console.print(
            Panel(
                f"Status: [{pb_color}]{playback_status.get('status', 'unknown')}[/{pb_color}]\n"
                f"File: {playback_status.get('filename') or 'none'}\n"
                f"Progress: {playback_status.get('messages_played', 0)}/{playback_status.get('total_messages', 0)}\n"
                f"Speed: {playback_status.get('speed', 1.0)}x\n"
                f"Loop: {playback_status.get('loop', False)}",
                title="LCM Playback",
            )
        )


def _format_size(size_bytes: int) -> str:
    """Format size in human readable form."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            if unit == "B":
                return f"{size_bytes} {unit}"
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
