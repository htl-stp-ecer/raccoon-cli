"""Update command - check for and install updates across all repos."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

import click
from rich.console import Console
from rich.prompt import Confirm

from raccoon.client.connection import (
    get_connection_manager,
    check_paramiko_version,
    ParamikoVersionError,
    print_paramiko_version_error,
)
from raccoon.project import find_project_root
from raccoon.version_checker import (
    PACKAGE_REGISTRY,
    PackageStatus,
    check_all_versions,
    check_gh_available,
    download_release_assets,
    render_version_table,
    write_remote_tracked_version,
)

logger = logging.getLogger("raccoon")


@click.command(name="update")
@click.option("--check", "check_only", is_flag=True, help="Only check, don't install")
@click.option("--laptop-only", is_flag=True, help="Only update laptop packages")
@click.option("--pi-only", is_flag=True, help="Only update Pi packages")
@click.option("--force", is_flag=True, help="Force reinstall even if versions match")
@click.pass_context
def update_command(
    ctx: click.Context,
    check_only: bool,
    laptop_only: bool,
    pi_only: bool,
    force: bool,
) -> None:
    """Check for and install updates across all packages.

    Checks GitHub releases for the latest versions and compares them
    against locally installed packages and Pi packages.

    Examples:
        raccoon update              # Update everything
        raccoon update --check      # Dry run, just show status
        raccoon update --laptop-only  # Only update laptop packages
        raccoon update --pi-only      # Only update Pi packages
    """
    console: Console = ctx.obj.get("console", Console())

    # Preflight: check gh CLI
    if not check_gh_available():
        console.print("[red]GitHub CLI (gh) is not installed.[/red]")
        console.print()
        console.print("Install it from: [cyan]https://cli.github.com/[/cyan]")
        console.print("  macOS:  [dim]brew install gh[/dim]")
        console.print("  Linux:  [dim]sudo apt install gh[/dim]  or  [dim]sudo dnf install gh[/dim]")
        console.print()
        console.print("Then authenticate: [cyan]gh auth login[/cyan]")
        raise SystemExit(1)

    # Get SSH client for Pi if needed
    ssh_client = None
    if not laptop_only:
        ssh_client = _get_ssh_client(console)

    # Check versions
    console.print("[dim]Checking versions...[/dim]")
    console.print()
    statuses = check_all_versions(ssh_client=ssh_client)
    any_outdated = render_version_table(console, statuses)

    if check_only:
        if any_outdated:
            console.print()
            console.print("Run [cyan]raccoon update[/cyan] to install updates.")
        else:
            console.print()
            console.print("[green]Everything is up to date.[/green]")
        return

    # Determine what to update
    laptop_updates = _get_laptop_updates(statuses, force) if not pi_only else []
    pi_updates = _get_pi_updates(statuses, force) if not laptop_only else []

    if not laptop_updates and not pi_updates:
        console.print()
        console.print("[green]Everything is up to date.[/green]")
        return

    # Show what will be updated
    console.print()
    if laptop_updates:
        names = ", ".join(s.info.name for s in laptop_updates)
        console.print(f"[cyan]Laptop updates:[/cyan] {names}")
    if pi_updates:
        names = ", ".join(s.info.name for s in pi_updates)
        console.print(f"[cyan]Pi updates:[/cyan] {names}")

    console.print()
    if not Confirm.ask("Proceed with updates?", default=True):
        console.print("[dim]Aborted.[/dim]")
        return

    # Perform updates
    if laptop_updates:
        _update_laptop(console, laptop_updates, force)

    if pi_updates:
        if ssh_client is None:
            console.print(
                "[yellow]Skipping Pi updates — not connected to a Pi.[/yellow]"
            )
        else:
            _update_pi(console, ssh_client, pi_updates, force)


def _get_ssh_client(console: Console):
    """Try to get an SSH client, auto-reconnecting from saved config if needed."""
    manager = get_connection_manager()

    if manager.is_connected:
        try:
            check_paramiko_version()
            return manager.get_ssh_client()
        except ParamikoVersionError as e:
            print_paramiko_version_error(e, console)
            return None
        except Exception as e:
            console.print(f"[yellow]Could not connect to Pi via SSH: {e}[/yellow]")
            return None

    # Try auto-reconnect from project or global config
    try:
        check_paramiko_version()
    except ParamikoVersionError as e:
        print_paramiko_version_error(e, console)
        return None

    pi_address = None
    pi_port = 8421
    pi_user = "pi"

    project_root = find_project_root()
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
        console.print("[dim]No known Pi — skipping Pi version checks.[/dim]")
        return None

    try:
        console.print(f"[dim]Connecting to {pi_address}...[/dim]")
        success = manager.connect_sync(pi_address, pi_port, pi_user)
        if success:
            return manager.get_ssh_client()
    except Exception as e:
        console.print(f"[yellow]Could not connect to Pi: {e}[/yellow]")
        return None

    console.print("[dim]Could not connect to Pi — skipping Pi version checks.[/dim]")
    return None


def _get_laptop_updates(
    statuses: list[PackageStatus], force: bool
) -> list[PackageStatus]:
    """Filter statuses to those needing laptop updates."""
    updates = []
    for s in statuses:
        if "laptop" not in s.info.targets:
            continue
        if s.latest_version is None:
            continue
        # Skip dev installs unless forced
        if s.laptop_version == "dev" and not force:
            continue
        if force or (s.laptop_version != s.latest_version):
            updates.append(s)
    return updates


def _get_pi_updates(
    statuses: list[PackageStatus], force: bool
) -> list[PackageStatus]:
    """Filter statuses to those needing Pi updates."""
    updates = []
    for s in statuses:
        if "pi" not in s.info.targets:
            continue
        if s.latest_version is None:
            continue
        if force or (s.pi_version != s.latest_version):
            updates.append(s)
    return updates


def _update_laptop(
    console: Console, updates: list[PackageStatus], force: bool
) -> None:
    """Update laptop packages by downloading wheels from GitHub releases."""
    console.print()
    console.print("[bold]Updating laptop packages...[/bold]")

    repo = "htl-stp-ecer/raccoon-cli"
    with tempfile.TemporaryDirectory() as tmpdir:
        console.print(f"[dim]Downloading raccoon wheel from {repo}...[/dim]")
        # Download only the raccoon wheel (not raccoon_transport)
        wheels = download_release_assets(repo, "raccoon-*.whl", tmpdir)
        if not wheels:
            console.print("[red]No raccoon wheel found in release.[/red]")
            return

        console.print(f"[dim]Installing {len(wheels)} wheel(s)...[/dim]")
        for w in wheels:
            console.print(f"  [dim]{os.path.basename(w)}[/dim]")

        pip_args = ["pip", "install"]
        if force:
            pip_args.append("--force-reinstall")
        pip_args.extend(wheels)

        result = subprocess.run(pip_args, capture_output=True, text=True)
        if result.returncode != 0:
            if "externally-managed-environment" in result.stderr:
                console.print("[yellow]System Python detected — retrying with --break-system-packages[/yellow]")
                pip_args.insert(2, "--break-system-packages")
                result = subprocess.run(pip_args, capture_output=True, text=True)
                if result.returncode != 0:
                    console.print(f"[red]pip install failed:[/red]\n{result.stderr.strip()}")
                    return
            else:
                console.print(f"[red]pip install failed:[/red]\n{result.stderr.strip()}")
                return

        console.print("[green]Laptop packages updated successfully.[/green]")


def _update_pi(
    console: Console,
    ssh_client,
    updates: list[PackageStatus],
    force: bool,
) -> None:
    """Update Pi packages by downloading assets and running install scripts."""
    console.print()
    console.print("[bold]Updating Pi packages...[/bold]")

    manager = get_connection_manager()
    pi_host = manager.state.pi_address
    pi_user = manager.state.pi_user

    # Group updates by repo to avoid duplicate downloads
    by_repo: dict[str, list[PackageStatus]] = {}
    for s in updates:
        by_repo.setdefault(s.info.repo, []).append(s)

    for repo, repo_updates in by_repo.items():
        _update_pi_repo(console, ssh_client, repo, repo_updates, pi_host, pi_user, force)


def _update_pi_repo(
    console: Console,
    ssh_client,
    repo: str,
    updates: list[PackageStatus],
    pi_host: str,
    pi_user: str,
    force: bool,
) -> None:
    """Update Pi packages from a single repo."""
    repo_short = repo.split("/")[-1]
    console.print(f"\n[cyan]Updating from {repo}...[/cyan]")

    with tempfile.TemporaryDirectory() as tmpdir:
        if repo == "htl-stp-ecer/raccoon-cli":
            # raccoon-cli has a server tarball with install.sh
            _update_pi_raccoon_cli(console, ssh_client, tmpdir, repo, pi_host, pi_user, force)
        else:
            # Other repos have tarballs with install.sh
            _update_pi_tarball(console, ssh_client, tmpdir, repo, repo_short, pi_host, pi_user)

        # Track versions for non-pip packages
        latest = updates[0].latest_version
        for s in updates:
            if s.info.pip_name is None and latest:
                write_remote_tracked_version(ssh_client, s.info.name, latest)


def _update_pi_raccoon_cli(
    console: Console,
    ssh_client,
    tmpdir: str,
    repo: str,
    pi_host: str,
    pi_user: str,
    force: bool,
) -> None:
    """Update raccoon server on Pi using the server tarball."""
    console.print("[dim]Downloading server tarball...[/dim]")
    tarballs = download_release_assets(repo, "raccoon-server-*.tar.gz", tmpdir)
    if not tarballs:
        console.print("[red]No server tarball found in release.[/red]")
        return

    tarball = tarballs[0]
    console.print(f"[dim]Extracting {os.path.basename(tarball)}...[/dim]")
    subprocess.run(["tar", "xzf", tarball, "-C", tmpdir], check=True)

    install_sh = _find_install_sh(tmpdir)
    if not install_sh:
        console.print("[red]install.sh not found in tarball.[/red]")
        return

    console.print(f"[dim]Running install.sh for Pi ({pi_host})...[/dim]")
    env = os.environ.copy()
    env["RPI_HOST"] = pi_host
    env["RPI_USER"] = pi_user

    result = subprocess.run(
        ["bash", install_sh],
        env=env,
        cwd=os.path.dirname(install_sh),
    )
    if result.returncode != 0:
        console.print("[red]install.sh failed.[/red]")
        return

    console.print("[green]raccoon server updated on Pi.[/green]")


def _update_pi_tarball(
    console: Console,
    ssh_client,
    tmpdir: str,
    repo: str,
    repo_short: str,
    pi_host: str,
    pi_user: str,
) -> None:
    """Update a Pi package by downloading its tarball and running install.sh."""
    console.print(f"[dim]Downloading release assets from {repo}...[/dim]")
    tarballs = download_release_assets(repo, "*.tar.gz", tmpdir)
    if not tarballs:
        console.print(f"[red]No tarball found for {repo_short}.[/red]")
        return

    tarball = tarballs[0]
    console.print(f"[dim]Extracting {os.path.basename(tarball)}...[/dim]")
    subprocess.run(["tar", "xzf", tarball, "-C", tmpdir], check=True)

    install_sh = _find_install_sh(tmpdir)
    if not install_sh:
        console.print(f"[red]install.sh not found in {repo_short} tarball.[/red]")
        return

    console.print(f"[dim]Running install.sh for {repo_short}...[/dim]")
    env = os.environ.copy()
    env["RPI_HOST"] = pi_host
    env["RPI_USER"] = pi_user

    result = subprocess.run(
        ["bash", install_sh],
        env=env,
        cwd=os.path.dirname(install_sh),
    )
    if result.returncode != 0:
        console.print(f"[red]install.sh failed for {repo_short}.[/red]")
        return

    console.print(f"[green]{repo_short} updated on Pi.[/green]")


def _find_install_sh(tmpdir: str) -> str | None:
    """Find install.sh at top level or one directory deep in tmpdir."""
    candidate = os.path.join(tmpdir, "install.sh")
    if os.path.exists(candidate):
        return candidate
    for entry in os.listdir(tmpdir):
        candidate = os.path.join(tmpdir, entry, "install.sh")
        if os.path.exists(candidate):
            return candidate
    return None
