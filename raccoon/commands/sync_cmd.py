"""Sync command - synchronize project files to Pi."""

import click
from rich.console import Console

from raccoon.client.conflict_resolver import (
    ConflictResolution,
    ConflictResolver,
    apply_resolution,
    prepare_conflict_files,
)
from raccoon.client.connection import (
    get_connection_manager,
    VersionMismatchError,
    print_version_mismatch_error,
    ParamikoVersionError,
    print_paramiko_version_error,
)
from raccoon.client.sftp_sync import SftpSync, SyncDirection, SyncOptions, load_raccoonignore
from raccoon.project import find_project_root, load_project_config


def do_sync(
    project_root,
    console: Console,
    direction: SyncDirection = SyncDirection.BIDIRECTIONAL,
    delete: bool = False,
    interactive: bool = True,
) -> tuple[bool, list[str]]:
    """
    Core sync logic - performs sync and optionally resolves conflicts interactively.

    Args:
        project_root: Path to the project root
        console: Rich console for output
        direction: Sync direction (PUSH, PULL, or BIDIRECTIONAL)
        delete: Whether to delete files not present on source side
        interactive: Whether to prompt for conflict resolution

    Returns:
        Tuple of (success, unresolved_conflicts)
    """
    # Load project config
    try:
        config = load_project_config(project_root)
        project_uuid = config.get("uuid")
        project_name = config.get("name", project_root.name)
    except Exception as e:
        console.print(f"[red]Error loading project config: {e}[/red]")
        return False, []

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
            return False, []
        console.print(f"[green]Connected to {manager.state.pi_hostname}[/green]")

    state = manager.state
    remote_path = f"/home/{state.pi_user}/programs/{project_uuid}"

    # Determine direction string for output
    if direction == SyncDirection.PUSH:
        direction_str = "pushing to"
    elif direction == SyncDirection.PULL:
        direction_str = "pulling from"
    else:
        direction_str = "syncing with"

    console.print(f"[cyan]Syncing '{project_name}' ({direction_str} {state.pi_hostname})...[/cyan]")

    # Perform sync
    try:
        ssh_client = manager.get_ssh_client()
        sync = SftpSync(ssh_client)

        # Load .raccoonignore patterns
        ignore_patterns = load_raccoonignore(project_root)

        # Set delete options based on direction
        if direction == SyncDirection.BIDIRECTIONAL:
            delete_remote = False
            delete_local = False
        elif direction == SyncDirection.PUSH:
            delete_remote = delete
            delete_local = False
        else:  # PULL
            delete_remote = False
            delete_local = delete

        options = SyncOptions(
            direction=direction,
            delete_remote=delete_remote,
            delete_local=delete_local,
        )
        if ignore_patterns:
            options.exclude_patterns = options.exclude_patterns + ignore_patterns

        result = sync.sync_with_progress(
            local_path=project_root,
            remote_path=remote_path,
            options=options,
        )

        console.print()

        if not result.success:
            console.print("[red]Sync failed[/red]")
            for error in result.errors:
                console.print(f"  - {error}")
            return False, []

        # Report results
        total_changes = result.files_uploaded + result.files_downloaded + result.files_deleted + result.files_auto_merged
        if total_changes == 0 and not result.conflicts:
            console.print("[green]Already in sync[/green]")
        else:
            console.print("[green]Sync complete![/green]")
            if result.files_uploaded > 0:
                console.print(f"  Uploaded:    {result.files_uploaded}")
            if result.files_downloaded > 0:
                console.print(f"  Downloaded:  {result.files_downloaded}")
            if result.files_auto_merged > 0:
                console.print(f"  Auto-merged: {result.files_auto_merged}")
            if result.files_deleted > 0:
                console.print(f"  Deleted:     {result.files_deleted}")

        if result.errors:
            console.print("[yellow]Warnings:[/yellow]")
            for error in result.errors:
                console.print(f"  - {error}")

        # Handle conflicts
        if result.conflicts:
            console.print()
            console.print(f"[yellow]{len(result.conflicts)} conflict(s) could not be auto-merged:[/yellow]")
            for conflict in result.conflicts:
                console.print(f"  - {conflict}")

            if interactive and click.confirm("\nResolve conflicts interactively?", default=True):
                _resolve_conflicts_interactively(
                    result.conflicts,
                    project_root,
                    remote_path,
                    manager,
                    console,
                )
                # Return empty conflicts if user resolved them
                return True, []
            else:
                return True, result.conflicts

        return True, []

    except Exception as e:
        console.print(f"[red]Sync error: {e}[/red]")
        return False, []


@click.command(name="sync")
@click.option("--force", "-f", is_flag=True, help="Force full sync (ignore hashes)")
@click.option("--push", is_flag=True, help="Push-only: upload local files to Pi")
@click.option("--pull", is_flag=True, help="Pull-only: download files from Pi to local")
@click.option("--delete", is_flag=True, help="Delete files not present on source side")
@click.pass_context
def sync_command(ctx: click.Context, force: bool, push: bool, pull: bool, delete: bool) -> None:
    """Sync the current project with the connected Pi.

    By default, performs bidirectional sync with hash-based change detection.
    Files changed locally are uploaded, files changed remotely are downloaded.
    Conflicts (both sides changed) are reported for manual resolution.

    Use --push for one-way upload (local -> Pi).
    Use --pull for one-way download (Pi -> local).
    Use --delete to remove files not present on the source side.
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
    elif push:
        direction = SyncDirection.PUSH
    elif pull:
        direction = SyncDirection.PULL
    else:
        direction = SyncDirection.BIDIRECTIONAL

    success, conflicts = do_sync(project_root, console, direction, delete, interactive=True)

    if not success:
        raise SystemExit(1)
    if conflicts:
        console.print("[dim]Resolve manually or use --force to overwrite[/dim]")


def _resolve_conflicts_interactively(
    conflicts: list[str],
    project_root,
    remote_path: str,
    manager,
    console: Console,
) -> None:
    """
    Interactively resolve sync conflicts.

    Args:
        conflicts: List of relative paths with conflicts
        project_root: Local project root
        remote_path: Remote project root
        manager: Connection manager
        console: Rich console for output
    """
    from raccoon.client.sftp_sync import HashCache, RemoteManifest

    try:
        ssh_client = manager.get_ssh_client()
        sftp = ssh_client.open_sftp()

        # Initialize hash cache and manifest for resolution
        hash_cache = HashCache(project_root)
        remote_manifest = RemoteManifest(sftp, remote_path)
        remote_manifest.load()

        # Prepare conflict files (download remote versions)
        console.print("\n[cyan]Downloading remote versions for comparison...[/cyan]")
        conflict_files = prepare_conflict_files(conflicts, project_root, remote_path, sftp)

        if not conflict_files:
            console.print("[yellow]Could not download remote files for comparison[/yellow]")
            sftp.close()
            return

        # Resolve conflicts interactively
        resolver = ConflictResolver()
        resolutions = resolver.resolve_conflicts(conflict_files, console)

        # Apply resolutions
        applied = 0
        skipped = 0
        errors = []

        for conflict in conflict_files:
            resolution = resolutions.get(conflict.rel_path, ConflictResolution.SKIP)

            if resolution == ConflictResolution.SKIP:
                skipped += 1
                continue

            error = apply_resolution(
                resolution,
                conflict,
                remote_path,
                sftp,
                hash_cache,
                remote_manifest,
            )

            if error:
                errors.append(error)
            else:
                applied += 1

        # Save caches
        hash_cache.save_cache()
        remote_manifest.save()
        sftp.close()

        # Report results
        console.print()
        if applied > 0:
            console.print(f"[green]Resolved {applied} conflict(s)[/green]")
        if skipped > 0:
            console.print(f"[yellow]Skipped {skipped} conflict(s)[/yellow]")
        if errors:
            console.print("[red]Errors during resolution:[/red]")
            for error in errors:
                console.print(f"  - {error}")

    except Exception as e:
        console.print(f"[red]Error during conflict resolution: {e}[/red]")


def sync_project_interactive(project_root, console: Console = None) -> bool:
    """
    Sync project with Pi, resolving conflicts interactively.

    Used by run and calibrate commands for auto-sync before execution.

    Returns:
        True if sync successful with no unresolved conflicts, False otherwise
    """
    console = console or Console()
    success, conflicts = do_sync(project_root, console, interactive=True)
    return success and not conflicts
