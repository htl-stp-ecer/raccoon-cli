"""Sync command - synchronize project files to Pi."""

import asyncio
import concurrent.futures
import getpass
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import click
import httpx
from rich.console import Console

from raccoon_cli.client.api import RemoteSyncState, create_api_client
from raccoon_cli.client.connection import (
    get_connection_manager,
    ParamikoVersionError,
    print_paramiko_version_error,
)
from raccoon_cli.git_history import create_pre_sync_snapshot
from raccoon_cli.client.sftp_sync import create_sync, SyncDirection, SyncOptions, load_raccoonignore
from raccoon_cli.fingerprint import FingerprintResult, compute_fingerprint, default_exclude_patterns
from raccoon_cli.project import find_project_root, load_project_config
from raccoon_cli.sync_state import SyncState as LocalSyncState
from raccoon_cli.sync_state import read_sync_state, write_sync_state


def _synced_by_label() -> str:
    """Short identifier for audit purposes: ``user@host``."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return f"{user}@{host}"


def _fingerprint_exclude_patterns(project_root: Path) -> list[str]:
    """Exclude list for fingerprinting: defaults + .raccoonignore (matches the server)."""
    patterns = default_exclude_patterns()
    patterns.extend(load_raccoonignore(project_root))
    return patterns


_T = TypeVar("_T")


def _run_coroutine_from_sync(make_coro: Callable[[], Awaitable[_T]]) -> _T:
    """Run an async coroutine factory from synchronous code, safely.

    ``raccoon sync`` is invoked from a plain Click callback (no event loop),
    but ``raccoon run`` wraps a call to ``do_sync`` inside its own
    ``asyncio.run(...)``, so by the time verification runs there is already
    a loop on this thread. Calling ``asyncio.run`` again in that case raises
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``.

    Detect the situation and offload to a worker thread with its own loop
    when necessary. The coroutine is created inside the worker so its owning
    loop is the one that will run it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: asyncio.run(make_coro()))
        return future.result()


def _format_diff(diff: dict[str, list[str]], limit: int = 10) -> list[str]:
    """Format a fingerprint diff into human-readable lines."""
    lines: list[str] = []
    for label, key in (
        ("only local", "only_in_self"),
        ("only remote", "only_in_other"),
        ("changed", "changed"),
    ):
        entries = diff.get(key, [])
        if not entries:
            continue
        shown = entries[:limit]
        extra = len(entries) - len(shown)
        for entry in shown:
            lines.append(f"  [{label}] {entry}")
        if extra > 0:
            lines.append(f"  [{label}] … and {extra} more")
    return lines


def do_sync(
    project_root,
    console: Console,
    direction: SyncDirection = SyncDirection.PUSH,
    delete: bool = True,
    update: bool = False,
    verbose: bool = False,
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

    if config.get("auto_checkpoints", True):
        target = f"{state.pi_user}@{state.pi_address}:{remote_path}"
        checkpoint_result = create_pre_sync_snapshot(project_root, direction.value, target)
        if checkpoint_result.created:
            console.print(f"[dim]Checkpoint {checkpoint_result.short_sha} saved[/dim]")
        elif checkpoint_result.reason == "not_git_repo":
            console.print("[dim]Checkpoint skipped (no .git repository).[/dim]")
        elif checkpoint_result.reason == "git_unavailable":
            console.print("[dim]Checkpoint skipped (git not installed).[/dim]")
        elif checkpoint_result.reason not in {"no_changes", ""}:
            console.print(f"[yellow]Warning: checkpoint failed ({checkpoint_result.error})[/yellow]")

    # Perform sync
    try:
        sync = create_sync(host=state.pi_address, user=state.pi_user)

        # Load .raccoonignore patterns
        ignore_patterns = load_raccoonignore(project_root)

        options = SyncOptions(
            direction=direction,
            delete=delete,
            update=update,
            verbose=verbose,
        )
        if ignore_patterns:
            options.exclude_patterns = options.exclude_patterns + ignore_patterns

        if verbose:
            backend_name = type(sync).__name__
            console.print(f"[dim]Backend: {backend_name}[/dim]")
            console.print(f"[dim]Local:   {project_root}[/dim]")
            console.print(f"[dim]Remote:  {state.pi_user}@{state.pi_address}:{remote_path}[/dim]")
            console.print(f"[dim]Exclude: {options.exclude_patterns}[/dim]")

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

        # Verify: hash both sides, require an exact match, bump the counter.
        verified = _verify_and_commit_sync(
            project_root=project_root,
            project_uuid=project_uuid,
            direction=direction,
            console=console,
        )
        return verified

    except Exception as e:
        console.print(f"[red]Sync error: {e}[/red]")
        return False


def _verify_and_commit_sync(
    project_root: Path,
    project_uuid: str,
    direction: SyncDirection,
    console: Console,
) -> bool:
    """Verify sync integrity by comparing content fingerprints.

    Runs regardless of the transfer backend (rsync or SFTP). On match, bumps the
    server sync counter and records the new state on both sides. On mismatch,
    prints a per-file diff and refuses to bump — callers should treat this as
    a failed sync even though the transfer itself returned success.
    """
    manager = get_connection_manager()
    state = manager.state
    if not state.api_token:
        console.print(
            "[yellow]Warning: cannot verify sync (no API token). "
            "Run 'raccoon connect' again to enable verification.[/yellow]"
        )
        return True  # transfer succeeded; we just couldn't verify

    patterns = _fingerprint_exclude_patterns(project_root)
    console.print("[cyan]Verifying sync integrity...[/cyan]")
    local_fp = compute_fingerprint(project_root, exclude_patterns=patterns)
    prev_state = read_sync_state(project_root)

    async def _run() -> bool:
        async with create_api_client(
            state.pi_address, state.pi_port, api_token=state.api_token
        ) as client:
            try:
                remote_fp = await client.get_fingerprint(project_uuid)
            except httpx.HTTPStatusError as e:
                console.print(
                    f"[red]Verify failed: server returned {e.response.status_code} "
                    f"fetching fingerprint ({e.response.text.strip()})[/red]"
                )
                return False
            except httpx.HTTPError as e:
                console.print(f"[red]Verify failed: {e}[/red]")
                return False

            if local_fp.root_hash != remote_fp.root_hash:
                console.print("[red]Fingerprint MISMATCH[/red]")
                console.print(
                    f"  local  : {local_fp.root_hash}  "
                    f"({local_fp.file_count} files, {local_fp.total_bytes} bytes)"
                )
                console.print(
                    f"  remote : {remote_fp.root_hash}  "
                    f"({remote_fp.file_count} files, {remote_fp.total_bytes} bytes)"
                )
                try:
                    remote_files = await client.get_fingerprint_files(project_uuid)
                except httpx.HTTPError:
                    remote_files = {}
                diff = local_fp.diff(
                    FingerprintResult(root_hash=remote_fp.root_hash, files=remote_files)
                )
                for line in _format_diff(diff):
                    console.print(line)
                console.print(
                    "[red]Sync is NOT trustworthy. Re-run 'raccoon sync' "
                    "or investigate the diff above.[/red]"
                )
                return False

            # Local and remote agree. Bump the counter (PUSH only) or
            # just snapshot the server's state (PULL).
            if direction == SyncDirection.PUSH:
                try:
                    current = await client.get_sync_state(project_uuid)
                except httpx.HTTPError as e:
                    console.print(f"[red]Failed to read sync state: {e}[/red]")
                    return False
                try:
                    new_state = await client.update_sync_state(
                        project_id=project_uuid,
                        fingerprint=local_fp.root_hash,
                        expected_prev_version=current.version,
                        synced_by=_synced_by_label(),
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 409:
                        console.print(
                            f"[red]Counter conflict: {e.response.json().get('detail', e.response.text)}[/red]"
                        )
                    else:
                        console.print(
                            f"[red]Failed to bump sync counter: {e.response.status_code} {e.response.text}[/red]"
                        )
                    return False
                except httpx.HTTPError as e:
                    console.print(f"[red]Failed to bump sync counter: {e}[/red]")
                    return False
            else:  # PULL: server state did not change, just snapshot it locally.
                try:
                    server_state = await client.get_sync_state(project_uuid)
                except httpx.HTTPError as e:
                    console.print(f"[red]Failed to read sync state: {e}[/red]")
                    return False
                new_state = RemoteSyncState(
                    version=server_state.version,
                    fingerprint=local_fp.root_hash,
                    synced_at=server_state.synced_at
                    or datetime.now(timezone.utc).isoformat(),
                    synced_by=server_state.synced_by,
                )

            write_sync_state(
                project_root,
                LocalSyncState(
                    version=new_state.version,
                    fingerprint=new_state.fingerprint,
                    synced_at=new_state.synced_at,
                    synced_by=new_state.synced_by,
                ),
            )
            # Show both hashes explicitly so devs can eyeball the match.
            console.print("[green]Fingerprints match[/green]")
            console.print(f"  local  : {local_fp.root_hash}")
            console.print(f"  remote : {remote_fp.root_hash}")
            console.print(
                f"  files  : {local_fp.file_count}  "
                f"bytes: {local_fp.total_bytes}"
            )
            if direction == SyncDirection.PUSH:
                console.print(
                    f"[green]Sync version: v{prev_state.version} → "
                    f"v{new_state.version}[/green]"
                )
            else:
                console.print(
                    f"[green]Sync version: v{new_state.version} (pulled)[/green]"
                )
            return True

    try:
        return _run_coroutine_from_sync(_run)
    except Exception as e:
        console.print(f"[red]Verify failed: {e}[/red]")
        return False


@click.command(name="sync")
@click.option("--push", is_flag=True, help="Push-only: upload local files to Pi")
@click.option("--pull", is_flag=True, help="Pull-only: download files from Pi to local")
@click.option("--delete/--no-delete", default=True, help="Delete extraneous files on destination (default: on)")
@click.option("--verbose", "-v", is_flag=True, help="Print detailed per-file sync actions")
@click.pass_context
def sync_command(ctx: click.Context, push: bool, pull: bool, delete: bool, verbose: bool) -> None:
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

    success = do_sync(project_root, console, direction, delete, verbose=verbose)

    if not success:
        raise SystemExit(1)


def sync_project_interactive(
    project_root,
    console: Console = None,
    direction: SyncDirection = SyncDirection.PUSH,
    update: bool = False,
) -> bool:
    """
    Sync project with Pi.

    Used by run and calibrate commands for auto-sync before/after execution.

    Args:
        project_root: Path to the project root
        console: Rich console for output
        direction: Sync direction (default PUSH)
        update: Skip files newer on destination (preserve local edits during pull)

    Returns:
        True if sync successful
    """
    console = console or Console()
    return do_sync(project_root, console, direction=direction, update=update)
