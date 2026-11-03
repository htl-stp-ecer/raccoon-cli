"""Connect command - establish connection to a Raccoon Pi."""

import asyncio

import click
from rich.console import Console
from rich.prompt import Prompt

from raccoon_cli.client.connection import (
    ParamikoVersionError,
    check_paramiko_version,
    get_connection_manager,
    print_paramiko_version_error,
    CLIENT_VERSION,
    _warn_version_mismatch,
)
from raccoon_cli.client.discovery import check_address
from raccoon_cli.client.ssh_connect import (
    ApiTokenError,
    SSHConnectError,
    open_ssh_session,
    read_api_token,
)
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

    ADDRESS is the IP address or hostname of the Pi. Logs in with your SSH
    key; if the Pi does not accept it yet, asks for the password once and
    installs the key (generating one if you have none). Then reads the
    raccoon-server API key from the Pi.

    Examples:
        raccoon connect 192.168.4.1
        raccoon connect raspberrypi.local
        raccoon connect 192.168.1.100 --port 8421
    """
    console: Console = ctx.obj.get("console", Console())

    try:
        check_paramiko_version()
    except ParamikoVersionError as e:
        print_paramiko_version_error(e, console)
        raise SystemExit(1)

    # 1. SSH: public key first, password + key deposit as fallback
    console.print(f"[cyan]Connecting to {user}@{address}...[/cyan]")
    try:
        ssh = open_ssh_session(
            address,
            user,
            ask_password=lambda: Prompt.ask(
                f"Password for {user}@{address}", password=True, console=console
            ),
            log=lambda msg: console.print(f"[dim]{msg}[/dim]"),
        )
    except SSHConnectError as e:
        console.print(f"[red]Failed to connect to {user}@{address}: {e}[/red]")
        raise SystemExit(1)
    console.print("[green]Connected![/green]")

    # 2. API key; without it nothing that talks to raccoon-server works
    console.print("[cyan]Retrieving API key...[/cyan]")
    try:
        token = read_api_token(ssh)
    except ApiTokenError as e:
        ssh.close()
        console.print(f"[red]Failed to retrieve the API key from {address}: {e}[/red]")
        console.print("[red]Aborting, connection not saved.[/red]")
        raise SystemExit(1)
    console.print("[green]API key retrieved.[/green]")

    # 3. Server info (hostname, version). Informational, the SSH side is already done.
    info = asyncio.run(check_address(address, port, retries=2))

    manager = get_connection_manager()
    manager.adopt_session(
        address=address,
        port=port,
        user=user,
        ssh_client=ssh,
        api_token=token,
        hostname=info.hostname if info else None,
        version=info.version if info else None,
    )
    state = manager.state

    console.print(f"  Host:    {state.pi_hostname or address}")
    console.print(f"  Address: {state.pi_address}:{state.pi_port}")
    if info:
        if info.version != CLIENT_VERSION:
            _warn_version_mismatch(CLIENT_VERSION, info.version)
        _warn_about_contract(console, state)
        console.print(f"  Version: {state.pi_version or 'unknown'}")
    else:
        console.print(
            f"[yellow]  raccoon-server did not answer on port {port}; "
            "start it on the Pi before using remote commands.[/yellow]"
        )

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


def _warn_about_contract(console: Console, state) -> None:
    """One red/yellow line when the Pi's transport contract is broken or in dev mode."""
    from raccoon_cli import contract_status

    summary = contract_status.fetch_remote_summary(state.pi_address, state.pi_port, state.api_token)
    if summary is None:
        return
    if not summary.get("ok", True):
        console.print(
            f"[red]  Transport contract: {', '.join(summary.get('blocking') or ['?'])} do not match the bundle. "
            "Runs will be refused; see `raccoon doctor`.[/red]"
        )
    for line in contract_status.dev_warnings(summary):
        console.print(f"[yellow]  {line}[/yellow]")
