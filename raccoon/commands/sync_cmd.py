"""Sync command - synchronize project files to Pi."""

import asyncio

import click
from rich.console import Console

from raccoon.client.connection import get_connection_manager
from raccoon.client.sftp_sync import SftpSync, SyncOptions, load_raccoonignore
from raccoon.project import find_project_root, load_project_config


@click.command(name="sync")
@click.option("--force", "-f", is_flag=True, help="Force full sync (ignore hashes)")
@click.option("--no-delete", is_flag=True, help="Don't delete remote files not in local")
@click.pass_context
def sync_command(ctx: click.Context, force: bool, no_delete: bool) -> None:
    """Sync the current project to the connected Pi.

    Uploads changed files via SFTP. Uses hash-based comparison
    to only upload files that have changed.
    """
    console: Console = ctx.obj.get("console", Console())

    # Check we're in a project
    project_root = find_project_root()
    if not project_root:
        console.print("[red]Error: Not in a Raccoon project directory[/red]")
        console.print("Run this command from within a project containing raccoon.project.yml")
        raise SystemExit(1)

    # Load project config
    try:
        config = load_project_config(project_root)
        project_uuid = config.get("uuid")
        project_name = config.get("name", project_root.name)
    except Exception as e:
        console.print(f"[red]Error loading project config: {e}[/red]")
        raise SystemExit(1)

    # Check connection
    manager = get_connection_manager()

    if not manager.is_connected:
        # Try to connect from project or global config
        project_conn = manager.load_from_project(project_root)
        if project_conn and project_conn.pi_address:
            console.print(f"[cyan]Connecting to Pi from project config: {project_conn.pi_address}...[/cyan]")
            success = manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
        else:
            known_pis = manager.load_known_pis()
            if known_pis:
                pi = known_pis[0]
                console.print(f"[cyan]Connecting to known Pi: {pi.get('address')}...[/cyan]")
                success = manager.connect_sync(pi.get("address"), pi.get("port", 8421))
            else:
                success = False

        if not success:
            console.print("[red]Error: Not connected to a Pi[/red]")
            console.print("Use [cyan]raccoon connect <PI_ADDRESS>[/cyan] to connect first")
            raise SystemExit(1)
        console.print(f"[green]Connected to {manager.state.pi_hostname}[/green]")

    state = manager.state

    # Determine remote path
    remote_path = f"/home/{state.pi_user}/programs/{project_uuid}"

    console.print(f"[cyan]Syncing project '{project_name}'...[/cyan]")
    console.print(f"  Local:  {project_root}")
    console.print(f"  Remote: {state.pi_address}:{remote_path}")
    console.print()

    # Perform sync
    try:
        ssh_client = manager.get_ssh_client()
        sync = SftpSync(ssh_client)

        # Load .raccoonignore patterns and merge with defaults
        ignore_patterns = load_raccoonignore(project_root)
        options = SyncOptions(delete_remote=not no_delete)
        if ignore_patterns:
            options.exclude_patterns = options.exclude_patterns + ignore_patterns

        result = sync.sync_with_progress(
            local_path=project_root,
            remote_path=remote_path,
            options=options,
        )

        console.print()

        if result.success:
            if result.files_uploaded == 0 and result.files_deleted == 0:
                console.print("[green]Already in sync - no changes needed[/green]")
            else:
                console.print(f"[green]Sync complete![/green]")
                console.print(f"  Files uploaded: {result.files_uploaded}")
                console.print(f"  Files deleted:  {result.files_deleted}")
                console.print(f"  Bytes transferred: {result.bytes_transferred:,}")

            if result.errors:
                console.print()
                console.print("[yellow]Warnings:[/yellow]")
                for error in result.errors:
                    console.print(f"  - {error}")
        else:
            console.print("[red]Sync failed[/red]")
            for error in result.errors:
                console.print(f"  - {error}")
            raise SystemExit(1)

    except Exception as e:
        console.print(f"[red]Sync error: {e}[/red]")
        raise SystemExit(1)


def sync_project_to_pi(project_root, console: Console = None) -> bool:
    """
    Utility function to sync project to Pi.

    Used by run and calibrate commands for auto-sync.

    Returns:
        True if sync successful, False otherwise
    """
    console = console or Console()

    # Load project config
    try:
        config = load_project_config(project_root)
        project_uuid = config.get("uuid")
    except Exception:
        return False

    # Check connection
    manager = get_connection_manager()

    if not manager.is_connected:
        return False

    state = manager.state
    remote_path = f"/home/{state.pi_user}/programs/{project_uuid}"

    try:
        ssh_client = manager.get_ssh_client()
        sync = SftpSync(ssh_client)

        # Load .raccoonignore patterns and merge with defaults
        ignore_patterns = load_raccoonignore(project_root)
        options = SyncOptions()
        if ignore_patterns:
            options.exclude_patterns = options.exclude_patterns + ignore_patterns

        result = sync.sync_with_progress(
            local_path=project_root,
            remote_path=remote_path,
            options=options,
        )
        return result.success
    except Exception:
        return False
