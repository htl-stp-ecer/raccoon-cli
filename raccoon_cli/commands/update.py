"""Update command - check for and install updates across all repos."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tarfile
import tempfile

import shutil

import click
from rich.console import Console
from rich.prompt import Confirm

from raccoon_cli.client.connection import (
    get_connection_manager,
    check_paramiko_version,
    ParamikoVersionError,
    print_paramiko_version_error,
)
from raccoon_cli.project import find_project_root
from raccoon_cli.version_checker import (
    PackageStatus,
    check_all_versions,
    download_release_assets,
    render_version_table,
    fetch_bundle_manifest,
    version_is_newer,
)

logger = logging.getLogger("raccoon")


@click.command(name="update")
@click.option("--check", "check_only", is_flag=True, help="Only check, don't install")
@click.option("--laptop-only", is_flag=True, help="Only update laptop packages")
@click.option("--pi-only", is_flag=True, help="Only update Pi packages")
@click.option("--force", is_flag=True, help="Force reinstall even if versions match")
@click.option("--bundle", "bundle_tag", default=None, metavar="NAME", help="Pin to a specific bundle (e.g. 2026.4.25.1)")
@click.option("--dev", "use_dev", is_flag=True, help="Use the dev manifest (latest component tips, auto-updated by CI)")
@click.pass_context
def update_command(
    ctx: click.Context,
    check_only: bool,
    laptop_only: bool,
    pi_only: bool,
    force: bool,
    bundle_tag: Optional[str],
    use_dev: bool,
) -> None:
    """Check for and install updates across all packages.

    Uses the raccoon-image bundle manifest to determine target versions.

    Examples:
        raccoon update              # Stable bundle (bundles/latest.json)
        raccoon update --dev        # Latest component tips (bundles/dev.json)
        raccoon update --bundle 2026.4.25.1  # Specific bundle
        raccoon update --check      # Dry run, just show status
    """
    console: Console = ctx.obj.get("console", Console())

    # Get SSH client for Pi if needed
    ssh_client = None
    server_url = None
    api_token = None
    if not laptop_only:
        ssh_client = _get_ssh_client(console)
        manager = get_connection_manager()
        if manager.state.pi_address:
            server_url = f"http://{manager.state.pi_address}:{manager.state.pi_port}"
            api_token = manager.state.api_token

    # Resolve which manifest to use
    if bundle_tag:
        resolved = bundle_tag
    elif use_dev:
        resolved = "dev"
    else:
        resolved = "latest"

    console.print(f"[dim]Fetching raccoon-image bundle ({resolved})...[/dim]")
    manifest = fetch_bundle_manifest(resolved)
    if manifest:
        console.print(f"[dim]Bundle: {manifest.get('bundle', resolved)}[/dim]")
    else:
        console.print("[yellow]Could not fetch raccoon-image manifest — falling back to individual repo versions.[/yellow]")
    console.print()

    # Check versions — prefer HTTP server endpoint, SSH as fallback
    console.print("[dim]Checking versions...[/dim]")
    console.print()
    statuses = check_all_versions(ssh_client=ssh_client, server_url=server_url, api_token=api_token, manifest=manifest)
    any_outdated, any_ahead = render_version_table(console, statuses)

    if check_only:
        console.print()
        if any_outdated:
            console.print("Run [cyan]raccoon update[/cyan] to install updates.")
        elif any_ahead:
            console.print("[blue]Some packages are ahead of the bundle.[/blue] Use [cyan]--dev[/cyan] or [cyan]--force[/cyan] to downgrade.")
        else:
            console.print("[green]Everything is up to date.[/green]")
        return

    # Determine what to update (exclude packages that are ahead of bundle)
    laptop_updates = _get_laptop_updates(statuses, force) if not pi_only else []
    pi_updates = _get_pi_updates(statuses, force) if not laptop_only else []

    if not laptop_updates and not pi_updates:
        console.print()
        if any_ahead:
            console.print("[blue]Some packages are ahead of the bundle[/blue] (shown in blue) — skipped.")
            console.print("Use [cyan]raccoon update --force[/cyan] to downgrade to bundle versions.")
        else:
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

    # Warn about skipped ahead-packages
    laptop_ahead = _get_laptop_ahead(statuses) if not pi_only else []
    pi_ahead = _get_pi_ahead(statuses) if not laptop_only else []
    if laptop_ahead or pi_ahead:
        ahead_names = ", ".join({s.info.name for s in laptop_ahead + pi_ahead})
        console.print(f"[blue]Ahead of bundle (skipped):[/blue] {ahead_names}")

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

    # After any Pi update, run post-install migrations via the bundled command.
    if pi_updates and ssh_client is not None:
        _run_post_install(console, ssh_client)


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
        console.print(f"[yellow]raccoon-server unreachable: {e}[/yellow]")

    # Fallback: raccoon-server is down but SSH may still work (e.g. broken server install).
    console.print(f"[dim]Trying direct SSH to {pi_address} (bypassing raccoon-server)...[/dim]")
    try:
        if manager.connect_ssh_only(pi_address, pi_user, pi_port):
            console.print("[yellow]Connected via SSH only — raccoon-server is down.[/yellow]")
            return manager.get_ssh_client()
    except Exception as e:
        console.print(f"[yellow]Direct SSH also failed: {e}[/yellow]")

    console.print("[dim]Could not connect to Pi — skipping Pi version checks.[/dim]")
    return None


def _get_laptop_updates(
    statuses: list[PackageStatus], force: bool
) -> list[PackageStatus]:
    """Filter statuses to those needing laptop updates (behind bundle only)."""
    updates = []
    for s in statuses:
        if "laptop" not in s.info.targets:
            continue
        if s.latest_version is None:
            continue
        if s.laptop_version == "dev" and not force:
            continue
        if s.laptop_version and not force and version_is_newer(s.laptop_version, s.latest_version):
            continue  # ahead of bundle — skip unless forced
        if force or (s.laptop_version != s.latest_version):
            updates.append(s)
    return updates


def _get_laptop_ahead(statuses: list[PackageStatus]) -> list[PackageStatus]:
    return [
        s for s in statuses
        if "laptop" in s.info.targets
        and s.laptop_version
        and s.latest_version
        and s.laptop_version not in {"dev"}
        and version_is_newer(s.laptop_version, s.latest_version)
    ]


def _get_pi_updates(
    statuses: list[PackageStatus], force: bool
) -> list[PackageStatus]:
    """Filter statuses to those needing Pi updates (behind bundle only)."""
    updates = []
    for s in statuses:
        if "pi" not in s.info.targets:
            continue
        if s.latest_version is None:
            continue
        if s.pi_version and not force and version_is_newer(s.pi_version, s.latest_version):
            continue  # ahead of bundle — skip unless forced
        if force or (s.pi_version != s.latest_version):
            updates.append(s)
    return updates


def _get_pi_ahead(statuses: list[PackageStatus]) -> list[PackageStatus]:
    return [
        s for s in statuses
        if "pi" in s.info.targets
        and s.pi_version
        and s.latest_version
        and version_is_newer(s.pi_version, s.latest_version)
    ]


def _update_laptop(
    console: Console, updates: list[PackageStatus], force: bool
) -> None:
    """Update laptop pip packages directly from PyPI."""
    console.print()
    console.print("[bold]Updating laptop packages...[/bold]")

    specs = [
        f"{s.info.pip_name}=={s.latest_version}"
        for s in updates
        if s.info.pip_name and s.info.on_pypi and s.latest_version
    ]
    skipped = [s for s in updates if not (s.info.pip_name and s.info.on_pypi)]
    for s in skipped:
        console.print(
            f"[yellow]Skipping {s.info.name} on laptop — not installable from PyPI.[/yellow]"
        )
    if not specs:
        return

    # Clean up stale .old exe from a previous Windows update
    if sys.platform == "win32":
        exe_path = shutil.which("raccoon")
        if exe_path:
            old_path = exe_path + ".old"
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass

    for spec in specs:
        console.print(f"  [dim]{spec}[/dim]")

    # On Windows, the running raccoon.exe is locked and pip cannot
    # overwrite it. Windows *does* allow renaming a locked file, so we
    # move the exe out of the way before pip install and clean up after.
    renamed_exe: str | None = None
    if sys.platform == "win32":
        exe_path = shutil.which("raccoon")
        if exe_path and os.path.isfile(exe_path):
            old_path = exe_path + ".old"
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
                os.rename(exe_path, old_path)
                renamed_exe = old_path
                logger.info(f"Renamed {exe_path} → {old_path} to avoid lock")
            except OSError:
                logger.debug("Could not rename raccoon exe, proceeding anyway")

    pip_args = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if force:
        pip_args.append("--force-reinstall")
    pip_args.extend(specs)

    result = subprocess.run(pip_args, capture_output=True, text=True)
    if result.returncode != 0:
        if "externally-managed-environment" in result.stderr:
            console.print("[yellow]System Python detected — retrying with --break-system-packages[/yellow]")
            pip_args.insert(4, "--break-system-packages")
            result = subprocess.run(pip_args, capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"[red]pip install failed:[/red]\n{result.stderr.strip()}")
                return
        else:
            console.print(f"[red]pip install failed:[/red]\n{result.stderr.strip()}")
            return

    # Clean up renamed exe on Windows
    if renamed_exe and os.path.exists(renamed_exe):
        try:
            os.remove(renamed_exe)
        except OSError:
            logger.debug(f"Could not remove {renamed_exe} — will be cleaned up next run")

    console.print("[green]Laptop packages updated successfully.[/green]")


def _update_pi(
    console: Console,
    ssh_client,
    updates: list[PackageStatus],
    force: bool,
) -> None:
    """Update Pi packages.

    Three paths depending on how the package is shipped:
    - PyPI pip packages: single ``pip3 install`` over SSH.
    - GitHub wheel (``on_pypi=False``): download wheel locally, SFTP to Pi,
      then ``pip3 install`` the uploaded wheel.
    - Non-pip packages: download the release tarball locally and run its
      ``install.py`` on the laptop (script SSHes to Pi using RPI_HOST/RPI_USER).
    """
    console.print()
    console.print("[bold]Updating Pi packages...[/bold]")

    manager = get_connection_manager()
    pi_host = manager.state.pi_address
    pi_user = manager.state.pi_user

    pypi_updates = [s for s in updates if s.info.pip_name and s.info.on_pypi]
    wheel_updates = [s for s in updates if s.info.pip_name and not s.info.on_pypi]
    tarball_updates = [s for s in updates if not s.info.pip_name]

    if pypi_updates:
        _pi_install_from_pypi(console, ssh_client, pypi_updates, force)

    if wheel_updates:
        _pi_install_github_wheels(console, ssh_client, wheel_updates, force)

    if tarball_updates:
        with tempfile.TemporaryDirectory() as tmpdir:
            by_repo: dict[str, list[PackageStatus]] = {}
            for s in tarball_updates:
                by_repo.setdefault(s.info.repo, []).append(s)
            for repo, repo_updates in by_repo.items():
                repo_short = repo.split("/")[-1]
                console.print(f"\n[cyan]Updating {repo_short} from {repo}...[/cyan]")
                _update_pi_tarball(console, tmpdir, repo, repo_short, pi_host, pi_user)


def _pi_pip_install(
    console: Console, ssh_client, install_args: list[str], force: bool
) -> bool:
    """Run ``pip3 install`` on the Pi with sudo/break-system-packages fallbacks.

    ``install_args`` are the args after ``install`` (e.g. pinned specs or wheel
    paths). Returns True on success.
    """
    force_flag = " --force-reinstall" if force else ""
    args_joined = " ".join(install_args)

    commands = [
        f"pip3 install --upgrade{force_flag} {args_joined}",
        f"sudo pip3 install --upgrade{force_flag} {args_joined}",
        f"sudo pip3 install --upgrade --break-system-packages{force_flag} {args_joined}",
    ]

    last_err = ""
    for cmd in commands:
        try:
            _, stdout, stderr = ssh_client.exec_command(cmd, timeout=300)
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace")
            if exit_code == 0:
                return True
            last_err = err
            if "externally-managed-environment" not in err and "Permission denied" not in err:
                # Unrelated failure — don't try escalating further
                break
        except Exception as e:
            last_err = str(e)
            break

    console.print(f"[red]pip3 install failed on Pi:[/red]\n{last_err.strip()}")
    return False


def _pi_install_from_pypi(
    console: Console,
    ssh_client,
    updates: list[PackageStatus],
    force: bool,
) -> None:
    """Install PyPI packages on the Pi via ``pip3 install`` over SSH."""
    specs = [
        f"{s.info.pip_name}=={s.latest_version}"
        for s in updates
        if s.info.pip_name and s.latest_version
    ]
    if not specs:
        return

    console.print("\n[cyan]Installing from PyPI on Pi...[/cyan]")
    for spec in specs:
        console.print(f"  [dim]{spec}[/dim]")

    if _pi_pip_install(console, ssh_client, specs, force):
        console.print("[green]PyPI packages updated on Pi.[/green]")


def _pi_install_github_wheels(
    console: Console,
    ssh_client,
    updates: list[PackageStatus],
    force: bool,
) -> None:
    """Download GitHub wheels for each package and pip-install them on the Pi."""
    console.print("\n[cyan]Installing GitHub wheels on Pi...[/cyan]")

    with tempfile.TemporaryDirectory() as tmpdir:
        remote_paths: list[str] = []
        sftp = ssh_client.open_sftp()
        try:
            for s in updates:
                pip_name = s.info.pip_name
                if not pip_name:
                    continue
                pattern = f"{pip_name.replace('-', '_')}-*.whl"
                console.print(f"[dim]Downloading {pip_name} wheels from {s.info.repo}...[/dim]")
                wheels = download_release_assets(s.info.repo, pattern, tmpdir)
                if not wheels:
                    console.print(f"[yellow]No wheel found for {s.info.name} — skipping.[/yellow]")
                    continue
                for local_path in wheels:
                    name = os.path.basename(local_path)
                    remote_path = f"/tmp/{name}"
                    console.print(f"  [dim]Uploading {name} → Pi:{remote_path}[/dim]")
                    sftp.put(local_path, remote_path)
                    remote_paths.append(remote_path)
        finally:
            sftp.close()

        if not remote_paths:
            console.print("[yellow]No wheels to install on Pi.[/yellow]")
            return

        success = _pi_pip_install(console, ssh_client, remote_paths, force)

        # Clean up uploaded wheels regardless of success
        ssh_client.exec_command("rm -f " + " ".join(remote_paths))

        if success:
            console.print("[green]GitHub wheels installed on Pi.[/green]")


def _update_pi_tarball(
    console: Console,
    tmpdir: str,
    repo: str,
    repo_short: str,
    pi_host: str,
    pi_user: str,
) -> None:
    """Update a Pi package by downloading its tarball and running its install script."""
    console.print(f"[dim]Downloading release assets from {repo}...[/dim]")
    tarballs = download_release_assets(repo, "*.tar.gz", tmpdir)
    if not tarballs:
        console.print(f"[red]No tarball found for {repo_short}.[/red]")
        return

    tarball = tarballs[0]
    console.print(f"[dim]Extracting {os.path.basename(tarball)}...[/dim]")
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(tmpdir)

    install_script = _find_install_script(tmpdir)
    if not install_script:
        console.print(f"[red]install script not found in {repo_short} tarball.[/red]")
        return

    console.print(f"[dim]Running {os.path.basename(install_script)} for {repo_short}...[/dim]")
    env = os.environ.copy()
    env["RPI_HOST"] = pi_host
    env["RPI_USER"] = pi_user

    result = subprocess.run(
        [sys.executable, install_script],
        env=env,
        cwd=os.path.dirname(install_script),
    )
    if result.returncode != 0:
        console.print(f"[red]{os.path.basename(install_script)} failed for {repo_short}.[/red]")
        return

    console.print(f"[green]{repo_short} updated on Pi.[/green]")


def _run_post_install(console: Console, ssh_client) -> None:
    """Run `raccoon-server post-install` on the Pi after any Pi package update."""
    console.print()
    console.print("[bold]Running post-install on Pi...[/bold]")

    try:
        _, stdout, stderr = ssh_client.exec_command(
            "sudo raccoon-server post-install", timeout=120
        )
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
    except Exception as e:
        console.print(f"[red]post-install failed: {e}[/red]")
        return

    if out:
        console.print(out)
    if exit_code != 0:
        console.print(f"[red]post-install failed (exit {exit_code}):[/red]\n{err}")
        return

    console.print("[green]Post-install complete.[/green]")


def _find_install_script(tmpdir: str) -> str | None:
    """Find install.py at top level or one directory deep in tmpdir."""
    candidate = os.path.join(tmpdir, "install.py")
    if os.path.exists(candidate):
        return candidate
    for entry in os.listdir(tmpdir):
        candidate = os.path.join(tmpdir, entry, "install.py")
        if os.path.exists(candidate):
            return candidate
    return None
