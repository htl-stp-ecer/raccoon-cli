"""Sync command - synchronize project files to Pi."""

import click
from rich.console import Console

from raccoon.client.connection import (
    get_connection_manager,
    VersionMismatchError,
    print_version_mismatch_error,
    ParamikoVersionError,
    print_paramiko_version_error,
)
from raccoon.client.sftp_sync import create_sync, SyncDirection, SyncOptions, load_raccoonignore
from raccoon.git_history import create_pre_sync_snapshot
from raccoon.project import find_project_root, load_project_config


def do_sync(
    project_root,
    console: Console,
    direction: SyncDirection = SyncDirection.PUSH,
    delete: bool = True,
) -> bool:
    """
    Core sync logic - performs sync using rsync.

    Args:
        project_root: Path to the project root
        console: Rich console for output
        direction: Sync direction (PUSH or PULL)
        delete: Whether to delete extraneous files on destination

    Returns:
        True if sync succeeded
    """
    # Load project config
    try:
        config = load_project_config(project_root)
        project_uuid = config.get("uuid")
        project_name = config.get("name", project_root.name)
    except Exception as e:
        console.print(f"[red]Error loading project config: {e}[/red]")
        return False

    # Check connection
    manager = get_connection_manager()

    if not manager.is_connected:
        # Try to connect from project or global config
        try:
            project_conn = manager.load_from_project(project_root)
            if project_conn and project_conn.pi_address:
                console.print(f"[cyan]Connecting to Pi: {project_conn.pi_address}...[/cyan]")
                success = manager.connect_sync(project_conn.pi_address, project_conn.pi_port, project_conn.pi_user)
            else:
                known_pis = manager.load_known_pis()
                if known_pis:
                    pi = known_pis[0]
                    console.print(f"[cyan]Connecting to known Pi: {pi.get('address')}...[/cyan]")
                    success = manager.connect_sync(pi.get("address"), pi.get("port", 8421))
                else:
                    success = False
        except ParamikoVersionError as e:
            print_paramiko_version_error(e, console)
            raise SystemExit(1)
        except VersionMismatchError as e:
            print_version_mismatch_error(e, console)
            raise SystemExit(1)

        if not success:
            console.print("[red]Error: Not connected to a Pi[/red]")
            console.print("Use [cyan]raccoon connect <PI_ADDRESS>[/cyan] to connect first")
            return False
        console.print(f"[green]Connected to {manager.state.pi_hostname}[/green]")

    state = manager.state
    remote_path = f"/home/{state.pi_user}/programs/{project_uuid}"

    # Determine direction string for output
    if direction == SyncDirection.PUSH:
        direction_str = "pushing to"
    else:
        direction_str = "pulling from"

    console.print(f"[cyan]Syncing '{project_name}' ({direction_str} {state.pi_hostname})...[/cyan]")

    snapshot_result = create_pre_sync_snapshot(
        project_root=project_root,
        direction=direction.value,
        target=f"{state.pi_user}@{state.pi_address}:{remote_path}",
    )
    if snapshot_result.created:
        console.print(
            f"[dim]Saved pre-sync snapshot {snapshot_result.commit_sha} ({snapshot_result.summary})[/dim]"
        )
    elif snapshot_result.reason == "not_git_repo":
        console.print("[dim]Local history snapshot skipped (no .git repository).[/dim]")
    elif snapshot_result.reason == "git_unavailable":
        console.print("[dim]Local history snapshot skipped (git not installed).[/dim]")
    elif snapshot_result.reason not in {"no_changes", ""}:
        console.print(f"[yellow]Warning: local history snapshot failed ({snapshot_result.error})[/yellow]")

    # Perform sync
    try:
        sync = create_sync(host=state.pi_address, user=state.pi_user)

        # Load .raccoonignore patterns
        ignore_patterns = load_raccoonignore(project_root)

        options = SyncOptions(
            direction=direction,
            delete=delete,
        )
        if ignore_patterns:
            options.exclude_patterns = options.exclude_patterns + ignore_patterns

        result = sync.sync(
            local_path=project_root,
            remote_path=remote_path,
            options=options,
        )

        console.print()

        if not result.success:
            console.print("[red]Sync failed[/red]")
            for error in result.errors:
                console.print(f"  - {error}")
            return False

        # Report results
        total_changes = result.files_uploaded + result.files_downloaded + result.files_deleted
        if total_changes == 0 and result.bytes_transferred == 0:
            console.print("[green]Already in sync[/green]")
        else:
            console.print("[green]Sync complete![/green]")
            if result.files_uploaded > 0:
                console.print(f"  Uploaded Files:  {result.files_uploaded}")
            if result.bytes_transferred > 0:
                console.print(f"  Bytes Total: {result.bytes_transferred}")
            if result.files_downloaded > 0:
                console.print(f"  Downloaded: {result.files_downloaded}")
            if result.files_deleted > 0:
                console.print(f"  Deleted:   {result.files_deleted}")

        if result.errors:
            console.print("[yellow]Warnings:[/yellow]")
            for error in result.errors:
                console.print(f"  - {error}")

        return True

    except Exception as e:
        console.print(f"[red]Sync error: {e}[/red]")
        return False


@click.command(name="sync")
@click.option("--push", is_flag=True, help="Push-only: upload local files to Pi")
@click.option("--pull", is_flag=True, help="Pull-only: download files from Pi to local")
@click.option("--delete/--no-delete", default=True, help="Delete extraneous files on destination (default: on)")
@click.pass_context
def sync_command(ctx: click.Context, push: bool, pull: bool, delete: bool) -> None:
    """Sync the current project with the connected Pi using rsync.

    By default, pushes local files to the Pi (local -> Pi).

    Use --push for explicit push (local -> Pi).
    Use --pull for pull (Pi -> local).
    Use --no-delete to keep extraneous files on the destination.
    """
    console: Console = ctx.obj.get("console", Console())

    # Check we're in a project
    project_root = find_project_root()
    if not project_root:
        console.print("[red]Error: Not in a Raccoon project directory[/red]")
        console.print("Run this command from within a project containing raccoon.project.yml")
        raise SystemExit(1)

    # Determine sync direction
    if push and pull:
        console.print("[red]Error: Cannot use --push and --pull together[/red]")
        raise SystemExit(1)
    elif pull:
        direction = SyncDirection.PULL
    else:
        direction = SyncDirection.PUSH

    success = do_sync(project_root, console, direction, delete)

    if not success:
        raise SystemExit(1)


def sync_project_interactive(
    project_root,
    console: Console = None,
    direction: SyncDirection = SyncDirection.PUSH,
) -> bool:
    """
    Sync project with Pi.

    Used by run and calibrate commands for auto-sync before/after execution.

    Args:
        project_root: Path to the project root
        console: Rich console for output
        direction: Sync direction (default PUSH)

    Returns:
        True if sync successful
    """
    console = console or Console()
    return do_sync(project_root, console, direction=direction)
