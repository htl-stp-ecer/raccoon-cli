"""Connect command - establish connection to a Raccoon Pi."""

import asyncio
from typing import Optional

import click
from rich.console import Console
from rich.prompt import Confirm

from raccoon_cli.client.connection import (
    get_connection_manager,
    ParamikoVersionError,
    print_paramiko_version_error,
)
from raccoon_cli.client.discovery import check_address
from raccoon_cli.project import find_project_root


@click.command(name="connect")
@click.argument("address", type=str)
@click.option("--port", "-p", type=int, default=8421, help="Pi server port")
@click.option("--user", "-u", type=str, default="pi", help="SSH username")
@click.option(
    "--save/--no-save", default=True, help="Save connection to project config"
)
@click.pass_context
def connect_command(
    ctx: click.Context,
    address: str,
    port: int,
    user: str,
    save: bool,
) -> None:
    """Connect to a Raccoon Pi server at ADDRESS.

    ADDRESS is the IP address or hostname of the Pi.

    Examples:
        raccoon connect 192.168.4.1
        raccoon connect raspberrypi.local
        raccoon connect 192.168.1.100 --port 8421
    """
    console: Console = ctx.obj.get("console", Console())

    # Check if the Pi is reachable
    console.print(f"[cyan]Checking connection to {address}:{port}...[/cyan]")
    result = asyncio.run(check_address(address, port))

    if not result:
        console.print(f"[red]Failed to connect to {address}:{port}[/red]")
        console.print("Make sure the Pi is running and raccoon-server is started.")
        return

    # Connect
    manager = get_connection_manager()
    try:
        success = asyncio.run(
            manager.connect(address=address, port=port, user=user)
        )
    except ParamikoVersionError as e:
        print_paramiko_version_error(e, console)
        raise SystemExit(1)

    if not success:
        console.print(f"[red]Failed to connect to {address}:{port}[/red]")
        return

    state = manager.state
    console.print(f"[green]Connected to {state.pi_hostname}[/green]")
    console.print(f"  Address: {state.pi_address}:{state.pi_port}")
    console.print(f"  Version: {state.pi_version or 'unknown'}")

    # Check API token status
    if state.api_token:
        console.print(f"  Auth:    [green]API token retrieved via SSH[/green]")
    else:
        # SSH key auth failed - offer to set up keys
        console.print(f"  Auth:    [yellow]SSH key authentication failed[/yellow]")
        console.print()

        if Confirm.ask("Set up SSH key authentication now?", default=True):
            _setup_ssh_and_retry(console, manager, address, user)
        else:
            console.print()
            console.print("[yellow]Warning: Without SSH key auth, remote commands will fail.[/yellow]")
            console.print(f"Run [cyan]raccoon connect {address}[/cyan] again to set up SSH keys.")

    # Save connection
    if save:
        # Always save to global config
        manager.save_to_global()
        console.print(f"  [dim]Saved to ~/.raccoon/config.yml[/dim]")

        # Save to project config if in a project
        project_root = find_project_root()
        if project_root:
            manager.save_to_project(project_root)
            console.print(f"  [dim]Saved to raccoon.project.yml[/dim]")


def _setup_ssh_and_retry(console: Console, manager, address: str, user: str) -> None:
    """Set up SSH key authentication and retry fetching the API token."""
    from raccoon_cli.client.ssh_keys import setup_ssh_key_interactive

    console.print()
    key = setup_ssh_key_interactive(address, user, console)

    if key:
        # Retry fetching the API token
        console.print()
        console.print("[dim]Fetching API token...[/dim]")

        token = manager._fetch_api_token_via_ssh(address, user)
        if token:
            manager._state.api_token = token
            console.print(f"  Auth:    [green]API token retrieved via SSH[/green]")
        else:
            console.print("[yellow]SSH key works but couldn't fetch API token.[/yellow]")
            console.print("[dim]The raccoon-server may not be fully configured on the Pi.[/dim]")


@click.command(name="disconnect")
@click.pass_context
def disconnect_command(ctx: click.Context) -> None:
    """Disconnect from the current Raccoon Pi."""
    console: Console = ctx.obj.get("console", Console())
    manager = get_connection_manager()

    if manager.is_connected:
        hostname = manager.state.pi_hostname
        manager.disconnect()
        console.print(f"[yellow]Disconnected from {hostname}[/yellow]")
    else:
        console.print("[dim]Not connected to any Pi[/dim]")
