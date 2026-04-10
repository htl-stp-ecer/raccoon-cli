"""Status command - show connection and project status."""

import asyncio
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from raccoon.client.connection import (
    get_connection_manager,
    check_paramiko_version,
    ParamikoVersionError,
)
from raccoon.client.api import create_api_client
from raccoon.project import find_project_root, load_project_config
from raccoon.version_checker import check_all_versions, render_version_table


@click.command(name="status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Show connection and project status.

    Displays:
    - Current Pi connection status
    - Local project information
    - Remote project sync status (if connected)
    """
    console: Console = ctx.obj.get("console", Console())

    manager = get_connection_manager()
    project_root = find_project_root()

    # Auto-reconnect from saved config before rendering anything
    if not manager.is_connected:
        _try_auto_connect(console, manager, project_root)

    state = manager.state

    # Connection status
    if state.connected:
        auth_status = "[green]authenticated[/green]" if state.api_token else "[yellow]no token[/yellow]"
        console.print(
            Panel(
                f"[green]Connected[/green] to [cyan]{state.pi_hostname}[/cyan]\n"
                f"Address: {state.pi_address}:{state.pi_port}\n"
                f"User: {state.pi_user}\n"
                f"Version: {state.pi_version or 'unknown'}\n"
                f"Auth: {auth_status}",
                title="Pi Connection",
            )
        )
    else:
        console.print(
            Panel(
                "[yellow]Not connected[/yellow]\n"
                "Use [cyan]raccoon connect[/cyan] to connect to a Pi",
                title="Pi Connection",
            )
        )

    # Local project status
    if project_root:
        try:
            config = load_project_config(project_root)
            project_name = config.get("name", project_root.name)
            project_uuid = config.get("uuid", "unknown")

            # Check for connection config in project
            conn_config = config.get("connection", {})
            saved_pi = conn_config.get("pi_address")

            console.print()
            console.print(
                Panel(
                    f"Name: [cyan]{project_name}[/cyan]\n"
                    f"UUID: {project_uuid}\n"
                    f"Path: {project_root}\n"
                    f"Saved Pi: {saved_pi or 'none'}",
                    title="Local Project",
                )
            )

            # If connected, show remote project status
            if state.connected:
                console.print()
                asyncio.run(_show_remote_status(console, state, project_uuid))

        except Exception as e:
            console.print(f"[red]Error loading project config: {e}[/red]")
    else:
        console.print()
        console.print("[dim]Not in a Raccoon project directory[/dim]")

    # Show known Pis
    known_pis = manager.load_known_pis()
    if known_pis:
        console.print()
        table = Table(title="Known Pis")
        table.add_column("Hostname", style="cyan")
        table.add_column("Address", style="green")
        table.add_column("Port")
        table.add_column("Last Seen", style="dim")

        for pi in known_pis:
            table.add_row(
                pi.get("hostname", "unknown"),
                pi.get("address", "unknown"),
                str(pi.get("port", 8421)),
                pi.get("last_seen", "unknown")[:10] if pi.get("last_seen") else "unknown",
            )

        console.print(table)

    # Package versions
    _show_package_versions(console, manager)


def _try_auto_connect(console, manager, project_root):
    """Try to auto-connect to a Pi from saved project or global config."""
    try:
        check_paramiko_version()
    except ParamikoVersionError:
        return

    pi_address = None
    pi_port = 8421
    pi_user = "pi"

    if project_root:
        project_conn = manager.load_from_project(project_root)
        if project_conn and project_conn.pi_address:
            pi_address = project_conn.pi_address
            pi_port = project_conn.pi_port
            pi_user = project_conn.pi_user

    if not pi_address:
        known_pis = manager.load_known_pis()
        if known_pis:
            pi = known_pis[0]
            pi_address = pi.get("address")
            pi_port = pi.get("port", 8421)

    if not pi_address:
        return

    try:
        console.print(f"[dim]Connecting to {pi_address}...[/dim]")
        manager.connect_sync(pi_address, pi_port, pi_user)
    except Exception:
        pass


def _show_package_versions(console: Console, manager) -> None:
    """Show package version information."""
    ssh_client = None
    if manager.is_connected:
        try:
            ssh_client = manager.get_ssh_client()
        except Exception:
            pass

    console.print()
    statuses = check_all_versions(ssh_client=ssh_client)
    any_outdated = render_version_table(console, statuses)

    if any_outdated:
        console.print()
        console.print("Run [cyan]raccoon update[/cyan] to install updates.")


async def _show_remote_status(console: Console, state, project_uuid: str) -> None:
    """Show remote project status on Pi."""
    try:
        async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
            # Check health
            health = await client.health()
            projects_dir = health.get("projects_dir", "unknown")

            # Try to find the project on Pi
            project = await client.get_project(project_uuid)

            if project:
                console.print(
                    Panel(
                        f"Name: [cyan]{project.name}[/cyan]\n"
                        f"Path: {project.path}\n"
                        f"Last Modified: {project.last_modified or 'unknown'}",
                        title="Remote Project (on Pi)",
                    )
                )
            else:
                console.print(
                    Panel(
                        f"[yellow]Project not found on Pi[/yellow]\n"
                        f"Use [cyan]raccoon sync[/cyan] to upload the project\n"
                        f"Projects directory: {projects_dir}",
                        title="Remote Project (on Pi)",
                    )
                )

    except Exception as e:
        console.print(f"[red]Failed to get remote status: {e}[/red]")
