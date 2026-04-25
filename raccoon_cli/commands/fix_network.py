"""fix-network command — deploy Wi-Fi fix services to a connected Pi via SSH."""

from __future__ import annotations

import click
from rich.console import Console

from raccoon_cli.client.connection import (
    get_connection_manager,
    ParamikoVersionError,
    print_paramiko_version_error,
)


@click.command(name="fix-network")
@click.argument("address", required=False)
@click.option("--user", "-u", default="pi", help="SSH username")
@click.pass_context
def fix_network_command(ctx: click.Context, address: str | None, user: str) -> None:
    """Deploy Wi-Fi power-save and gratuitous-ARP fixes to a Pi.

    Uses the current connection if ADDRESS is omitted.
    """
    console: Console = ctx.obj["console"]

    if address is None:
        manager = get_connection_manager()
        if not manager.is_connected:
            console.print("[red]Not connected to a Pi. Provide ADDRESS or run raccoon connect first.[/red]")
            raise SystemExit(1)
        address = manager.state.pi_address
        user = manager.state.pi_user or user

    console.print(f"[cyan]Deploying network fixes to {address}...[/cyan]")

    try:
        import paramiko
    except ImportError:
        console.print("[red]paramiko is required for SSH. Run: pip install paramiko[/red]")
        raise SystemExit(1)

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=address, username=user)

        _, stdout, stderr = client.exec_command("sudo raccoon-server fix-network")
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        exit_code = stdout.channel.recv_exit_status()

        if out:
            console.print(out)
        if err:
            console.print(f"[dim]{err}[/dim]")

        if exit_code == 0:
            console.print("[green]Network fixes deployed successfully.[/green]")
        else:
            console.print(f"[red]Command exited with code {exit_code}[/red]")
            raise SystemExit(exit_code)

    except ParamikoVersionError as e:
        print_paramiko_version_error(e, console)
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]SSH failed: {e}[/red]")
        raise SystemExit(1)
    finally:
        client.close()
