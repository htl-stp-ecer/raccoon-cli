"""Update command - check for and install updates across all repos."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from typing import Optional

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
from raccoon_cli import laptop_update
from raccoon_cli.project import ProjectError, find_project_root
from raccoon_cli.version_checker import (
    PackageStatus,
    check_all_versions,
    download_release_assets_for_version,
    render_version_table,
    fetch_bundle_manifest,
    version_is_newer,
    resolve_pypi_version,
)

logger = logging.getLogger("raccoon")


def _error_badge(console: Console, message: str) -> None:
    """Print a clean ERROR badge followed by the message (no traceback)."""
    console.print(f"[bold white on red] ERROR [/bold white on red] {message}")


@click.command(name="update")
@click.option("--check", "check_only", is_flag=True, help="Only check, don't install")
@click.option("--laptop-only", is_flag=True, help="Only update laptop packages")
@click.option("--pi-only", is_flag=True, help="Only update Pi packages")
@click.option("--force", is_flag=True, help="Force reinstall even if versions match")
@click.option("--bundle", "bundle_tag", default=None, metavar="NAME", help="Pin to a specific bundle")
@click.option("--dev", "use_dev", is_flag=True, help="Use the dev manifest (latest component tips, auto-updated by CI)")
@click.option(
    "--allow-missing-pypi-version-fallback",
    is_flag=True,
    help="If the requested bundle version is missing on PyPI, install the latest available PyPI release instead.",
)
@click.pass_context
def update_command(
    ctx: click.Context,
    check_only: bool,
    laptop_only: bool,
    pi_only: bool,
    force: bool,
    bundle_tag: Optional[str],
    use_dev: bool,
    allow_missing_pypi_version_fallback: bool,
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

    # The update needs a project (laptop pyproject.toml, project Pi connection).
    # Fail fast with a clean message instead of a traceback or a slow Pi connect.
    try:
        find_project_root()
    except ProjectError:
        _error_badge(
            console,
            "Not in a raccoon project (no raccoon.project.yml found). "
            "Run [cyan]raccoon update[/cyan] from inside a project directory.",
        )
        ctx.exit(1)

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

    # No Pi to talk to: behave exactly like --laptop-only (no Pi column, no Pi
    # updates, no Pi version lookups) instead of listing Pi packages as unknown.
    if not laptop_only and not pi_only and ssh_client is None:
        console.print("[yellow]Could not connect to the Pi — checking laptop packages only.[/yellow]")
        laptop_only = True
        server_url = None
        api_token = None

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
        # Before anything is looked at or touched: a bundle without a transport version
        # cannot be installed consistently. (The dev bundle is the deliberate exception.)
        problem = missing_transport_problem(manifest, resolved)
        if problem:
            _error_badge(console, problem)
            ctx.exit(1)
    else:
        console.print("[yellow]Could not fetch raccoon-image manifest — falling back to individual repo versions.[/yellow]")

    # Check versions — prefer HTTP server endpoint, SSH as fallback
    console.print("[dim]Checking versions...[/dim]")
    statuses = check_all_versions(ssh_client=ssh_client, server_url=server_url, api_token=api_token, manifest=manifest)
    any_outdated, any_ahead = render_version_table(
        console, statuses, show_laptop=not pi_only, show_pi=not laptop_only
    )

    # The fallback to individual repo versions must not skip the transport check either.
    if manifest is None and resolved != "dev":
        transport = transport_status(statuses)
        if transport is None or not transport.latest_version:
            _error_badge(
                console,
                "Could not determine the raccoon-transport version to install "
                "(no bundle manifest and no GitHub release). Nothing was changed.",
            )
            ctx.exit(1)

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
        _update_laptop(
            console,
            laptop_updates,
            force,
            allow_missing_pypi_version_fallback,
            bundle_version=(manifest or {}).get("bundle"),
        )

    if pi_updates:
        if ssh_client is None:
            console.print(
                "[yellow]Skipping Pi updates — not connected to a Pi.[/yellow]"
            )
        else:
            _update_pi(
                console,
                ssh_client,
                pi_updates,
                force,
                allow_missing_pypi_version_fallback,
                transport=None if resolved == "dev" else transport_status(statuses),
            )

    # After any Pi update, run post-install migrations via the bundled command.
    if pi_updates and ssh_client is not None:
        _run_post_install(console, ssh_client)

        # Order matters: all binaries and packages first (above), then the bundle file,
        # then the restart. A service must never start against a bundle file that names
        # another transport than the one just installed. A dev update has no contract of
        # its own: it writes `dev` as the version of exactly the components it installed
        # and leaves the rest of the file (other versions, contract_hash) as it is.
        if resolved == "dev":
            if _mark_components_dev(console, ssh_client, [s.info.name for s in pi_updates], manifest):
                _restart_pi_services(console, ssh_client)
        elif manifest:
            if _install_bundle_file(console, ssh_client, manifest):
                _restart_pi_services(console, ssh_client)


def missing_transport_problem(manifest: dict, resolved: str) -> Optional[str]:
    """Message when a bundle has no raccoon-transport version, else None.

    ``dev`` is exempt: it points at the newest components, has no transport field,
    and runs without a transport check or pin (transport comes in as a dependency).
    """
    if resolved == "dev" or manifest.get("bundle") == "dev":
        return None
    components = manifest.get("components") or {}
    if not str(components.get("raccoon-transport") or "").strip():
        return (
            f"Bundle {manifest.get('bundle', resolved)} has no raccoon-transport version. "
            "Components of one bundle must share one transport, so nothing was changed. "
            "Pick another bundle, or use [cyan]--dev[/cyan] for the unchecked newest components."
        )
    contract_hash = manifest.get("contract_hash")
    if not isinstance(contract_hash, str) or not contract_hash.strip() or contract_hash.strip() == "dev":
        return (
            f"Bundle {manifest.get('bundle', resolved)} has no usable contract_hash, so the Pi could not "
            "be told which transport to expect. Nothing was changed."
        )
    return None


def transport_status(statuses: list[PackageStatus]) -> Optional[PackageStatus]:
    for s in statuses:
        if s.info.name == "raccoon-transport":
            return s
    return None


def with_transport_pin(specs: list[str], transport: Optional[PackageStatus], resolve) -> list[str]:
    """Add ``raccoon-transport==<bundle version>`` to a pip spec list unless already there."""
    if transport is None or not transport.latest_version:
        return specs
    if any(spec.split("==")[0].split("[")[0] == "raccoon-transport" for spec in specs):
        return specs
    resolved, _note = resolve(transport)
    if not resolved:
        return specs
    return [*specs, f"raccoon-transport=={resolved}"]


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

    try:
        project_root = find_project_root()
    except ProjectError:
        project_root = None  # not in a project — fall back to the global known Pis
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
        console.print("[yellow]raccoon-server unreachable.[/yellow]")
    except Exception as e:
        console.print(f"[yellow]raccoon-server unreachable: {e}[/yellow]")

    console.print(
        "[dim]Could not connect to Pi — skipping Pi version checks. "
        "If raccoon-server is broken, flash the latest raccoon-image.[/dim]"
    )
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
        if "pi" not in s.info.targets or s.info.pi_install == "dependency":
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
    console: Console,
    updates: list[PackageStatus],
    force: bool,
    allow_missing_pypi_version_fallback: bool,
    bundle_version: Optional[str] = None,
) -> None:
    """Pin the bundle versions in the project's pyproject.toml and run ``uv sync``."""
    console.print()
    console.print("[bold]Updating laptop packages...[/bold]")

    try:
        project_root = find_project_root()
    except ProjectError:
        _error_badge(
            console,
            "Not in a raccoon project (no raccoon.project.yml found). "
            "Run [cyan]raccoon update[/cyan] inside a project to update the laptop packages, "
            "or use [cyan]--pi-only[/cyan].",
        )
        return
    if not (project_root / "pyproject.toml").exists():
        _error_badge(
            console,
            f"No pyproject.toml in {project_root} — run [cyan]raccoon migrate[/cyan] first.",
        )
        return

    uv = shutil.which("uv")
    if not uv:
        _error_badge(
            console,
            "uv is not installed or not in PATH — skipping laptop update.\n"
            "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh",
        )
        return

    pins: dict[str, str] = {}
    for s in updates:
        if not (s.info.pip_name and s.latest_version):
            continue
        try:
            resolved, note = resolve_pypi_version(
                s.info.pip_name,
                s.latest_version,
                allow_missing_fallback=allow_missing_pypi_version_fallback,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if note:
            console.print(f"[yellow]{note}[/yellow]")
        if resolved:
            pins[s.info.pip_name] = resolved
    if not pins:
        return

    pyproject = project_root / "pyproject.toml"
    changed, missing = laptop_update.apply_pins(pyproject, pins, bundle_version)
    for dist in changed:
        console.print(f"  [dim]{dist}=={pins[dist]}[/dim]")
    for dist in missing:
        console.print(f"  [yellow]{dist} is not a dependency in pyproject.toml — not pinned.[/yellow]")
    if bundle_version and not laptop_update.is_valid_project_version(bundle_version):
        console.print(f"  [dim]Bundle '{bundle_version}' is not a valid version — project version left unchanged.[/dim]")
    if not changed:
        return

    if not laptop_update.can_sync_while_running(project_root):
        console.print(
            "[yellow]raccoon is running from this project's environment, which Windows "
            "locks while running.[/yellow]\n"
            f"pyproject.toml is updated — run [cyan]uv sync --refresh{' --reinstall' if force else ''}[/cyan] "
            f"in {project_root} to finish."
        )
        return

    # --refresh: a just-released version may be missing from uv's cached index.
    cmd = [uv, "sync", "--refresh"] + (["--reinstall"] if force else [])
    result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True)
    if result.returncode != 0:
        _error_badge(console, f"uv sync failed:\n{result.stderr.strip()}")
        return

    console.print("[green]Laptop packages updated successfully.[/green]")


def _update_pi(
    console: Console,
    ssh_client,
    updates: list[PackageStatus],
    force: bool,
    allow_missing_pypi_version_fallback: bool,
    transport: Optional[PackageStatus] = None,
) -> None:
    """Update Pi packages, each by the method declared in the registry.

    - ``pypi``:    ``pip3 install`` over SSH (raccoon-lib + sim extra).
    - ``tarball``: download the release tarball locally, run its ``install.py``
      on the laptop (it SSHes to the Pi via RPI_HOST/RPI_USER).
    - ``deb``:     download the .deb, SFTP it to the Pi, ``apt install`` it.
    """
    console.print()
    console.print("[bold]Updating Pi packages...[/bold]")

    manager = get_connection_manager()
    pi_host = manager.state.pi_address
    pi_user = manager.state.pi_user

    pypi_updates = [s for s in updates if s.info.pi_install == "pypi"]
    if pypi_updates:
        _pi_install_from_pypi(
            console,
            ssh_client,
            pypi_updates,
            force,
            allow_missing_pypi_version_fallback,
            transport=transport,
        )

    for s in updates:
        if s.info.pi_install == "deb":
            _pi_install_deb(console, ssh_client, s)
        elif s.info.pi_install == "tarball":
            _update_pi_tarball(console, s, pi_host, pi_user)


def _pi_pip_install(
    console: Console, ssh_client, install_args: list[str], force: bool
) -> tuple[bool, str]:
    """Run ``pip3 install`` on the Pi with sudo/break-system-packages fallbacks.

    ``install_args`` are the args after ``install`` (e.g. pinned specs or wheel
    paths). Returns True on success.
    """
    force_flag = " --force-reinstall" if force else ""
    args_joined = " ".join(shlex.quote(a) for a in install_args)

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
                return True, ""
            last_err = err
            if "externally-managed-environment" not in err and "Permission denied" not in err:
                # Unrelated failure — don't try escalating further
                break
        except Exception as e:
            last_err = str(e)
            break
    return False, last_err


def _pi_install_from_pypi(
    console: Console,
    ssh_client,
    updates: list[PackageStatus],
    force: bool,
    allow_missing_pypi_version_fallback: bool,
    transport: Optional[PackageStatus] = None,
) -> None:
    """Install PyPI packages on the Pi via ``pip3 install`` over SSH.

    The transport is always pinned exactly in the same pip run (``transport`` is None
    only for the dev bundle, which has no transport and runs without a pin), so the
    resolver cannot pull a newer transport than the bundle names.
    """
    specs: list[str] = []
    for s in updates:
        if not (s.info.pip_name and s.latest_version):
            continue
        try:
            resolved, note = resolve_pypi_version(
                s.info.pip_name,
                s.latest_version,
                allow_missing_fallback=allow_missing_pypi_version_fallback,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if note:
            console.print(f"[yellow]{note}[/yellow]")
        if resolved:
            extras = f"[{s.info.pi_extras}]" if s.info.pi_extras else ""
            specs.append(f"{s.info.pip_name}{extras}=={resolved}")
    if not specs:
        return
    specs = with_transport_pin(
        specs, transport, resolve=lambda s: resolve_pypi_version(
            s.info.pip_name, s.latest_version, allow_missing_fallback=allow_missing_pypi_version_fallback
        )
    )

    console.print("\n[cyan]Installing from PyPI on Pi...[/cyan]")
    for spec in specs:
        console.print(f"  [dim]{spec}[/dim]")

    success, err = _pi_pip_install(console, ssh_client, specs, force)
    if success:
        console.print("[green]PyPI packages updated on Pi.[/green]")
        return

    console.print("[yellow]Pi PyPI install failed — trying offline wheel install.[/yellow]")
    if _pi_install_from_local_wheels(console, ssh_client, specs, force):
        console.print("[green]PyPI packages updated on Pi.[/green]")
        return

    console.print(f"[red]pip3 install failed on Pi:[/red]\n{err.strip()}")


def _pi_install_deb(console: Console, ssh_client, status: PackageStatus) -> None:
    """Download a release .deb, upload it to the Pi and ``apt install`` it."""
    info = status.info
    console.print(f"\n[cyan]Updating {info.name} from {info.repo}...[/cyan]")
    with tempfile.TemporaryDirectory() as tmpdir:
        debs = download_release_assets_for_version(
            info.repo, info.pi_asset or "*.deb", tmpdir, status.latest_version
        )
        if not debs:
            console.print(f"[red]No .deb found for {info.name} {status.latest_version}.[/red]")
            return

        name = os.path.basename(debs[0])
        remote_path = f"/tmp/{name}"
        console.print(f"  [dim]Uploading {name} → Pi:{remote_path}[/dim]")
        sftp = ssh_client.open_sftp()
        try:
            sftp.put(debs[0], remote_path)
        finally:
            sftp.close()

        try:
            quoted = shlex.quote(remote_path)
            _, stdout, stderr = ssh_client.exec_command(
                f"sudo apt install -y {quoted}", timeout=300
            )
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace")
        except Exception as e:
            console.print(f"[red]apt install failed: {e}[/red]")
            return
        finally:
            try:
                ssh_client.exec_command(f"rm -f {shlex.quote(remote_path)}")
            except Exception:
                pass

    if exit_code != 0:
        console.print(f"[red]apt install failed for {info.name}:[/red]\n{err.strip()}")
        return
    console.print(f"[green]{info.name} updated on Pi.[/green]")


def _pi_install_from_local_wheels(
    console: Console,
    ssh_client,
    specs: list[str],
    force: bool,
) -> bool:
    with tempfile.TemporaryDirectory() as tmpdir:
        wheels = _download_pypi_wheels_for_pi(console, ssh_client, specs, tmpdir)
        if not wheels:
            return False

        remote_paths: list[str] = []
        sftp = ssh_client.open_sftp()
        try:
            for local_path in wheels:
                name = os.path.basename(local_path)
                remote_path = f"/tmp/{name}"
                console.print(f"  [dim]Uploading {name} → Pi:{remote_path}[/dim]")
                sftp.put(local_path, remote_path)
                remote_paths.append(remote_path)
        finally:
            sftp.close()

        success, _ = _pi_pip_install(console, ssh_client, remote_paths, force)
        ssh_client.exec_command("rm -f " + " ".join(shlex.quote(p) for p in remote_paths))
        return success


def _download_pypi_wheels_for_pi(
    console: Console,
    ssh_client,
    specs: list[str],
    tmpdir: str,
) -> list[str]:
    py_tag, impl_tag, abi_tag, platforms = _get_pi_wheel_tags(ssh_client)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--only-binary=:all:",
        "-d",
        tmpdir,
        "--python-version",
        py_tag,
        "--implementation",
        impl_tag,
        "--abi",
        abi_tag,
    ]
    for platform_tag in platforms:
        cmd.extend(["--platform", platform_tag])
    cmd.extend(specs)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]pip download failed:[/red]\n{result.stderr.strip()}")
        return []

    wheels = [
        os.path.join(tmpdir, name)
        for name in os.listdir(tmpdir)
        if name.endswith(".whl")
    ]
    if not wheels:
        console.print("[red]No wheels downloaded for Pi.[/red]")
    return wheels


def _get_pi_wheel_tags(ssh_client) -> tuple[str, str, str, list[str]]:
    py_tag = "311"
    impl_tag = "cp"
    abi_tag = "cp311"
    platforms = ["manylinux2014_aarch64", "linux_aarch64"]
    try:
        _, stdout, _ = ssh_client.exec_command(
            "python3 -c \"import platform,sys; "
            "print(f'{sys.version_info.major}{sys.version_info.minor}'); "
            "print(sys.implementation.name); "
            "print(platform.machine())\"",
            timeout=10,
        )
        lines = stdout.read().decode(errors="replace").splitlines()
        if len(lines) >= 1 and lines[0].strip():
            py_tag = lines[0].strip()
            abi_tag = f"cp{py_tag}"
        if len(lines) >= 2 and lines[1].strip():
            impl = lines[1].strip().lower()
            impl_tag = "cp" if impl.startswith("cpython") else impl[:2]
            abi_tag = f"{impl_tag}{py_tag}"
        if len(lines) >= 3 and lines[2].strip():
            machine = lines[2].strip().lower()
            if "armv7" in machine:
                platforms = ["manylinux2014_armv7l", "linux_armv7l"]
            elif "arm" in machine and "64" in machine:
                platforms = ["manylinux2014_aarch64", "linux_aarch64"]
    except Exception:
        pass
    return py_tag, impl_tag, abi_tag, platforms


def _update_pi_tarball(
    console: Console,
    status: PackageStatus,
    pi_host: str,
    pi_user: str,
) -> None:
    """Update a Pi package by downloading its tarball and running its install script."""
    info = status.info
    repo_short = info.repo.split("/")[-1]
    console.print(f"\n[cyan]Updating {info.name} from {info.repo}...[/cyan]")

    with tempfile.TemporaryDirectory() as tmpdir:
        console.print(f"[dim]Downloading release assets from {info.repo}...[/dim]")
        tarballs = download_release_assets_for_version(
            info.repo, info.pi_asset or "*.tar.gz", tmpdir, status.latest_version
        )
        if not tarballs:
            console.print(f"[red]No tarball found for {repo_short} {status.latest_version}.[/red]")
            return

        # Each package is extracted into its own directory so one package's
        # install.py is never mistaken for another's.
        extract_dir = os.path.join(tmpdir, info.name)
        os.makedirs(extract_dir)
        console.print(f"[dim]Extracting {os.path.basename(tarballs[0])}...[/dim]")
        with tarfile.open(tarballs[0], "r:gz") as tf:
            tf.extractall(extract_dir, filter="data")

        install_script = _find_install_script(extract_dir)
        if not install_script:
            console.print(f"[red]install script not found in {repo_short} tarball.[/red]")
            return

        console.print(f"[dim]Running {os.path.basename(install_script)} for {repo_short}...[/dim]")
        env = os.environ.copy()
        env["RPI_HOST"] = pi_host
        env["RPI_USER"] = pi_user
        # The CLI's own Paramiko connection (AutoAddPolicy, no known_hosts) already
        # reached the Pi; give the release's install.py the same host-key policy,
        # otherwise system ssh aborts after every re-flash ("host key changed").
        env.setdefault(
            "RPI_SSH_OPTS",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR",
        )

        result = subprocess.run(
            [sys.executable, install_script],
            env=env,
            cwd=os.path.dirname(install_script),
        )
        if result.returncode != 0:
            console.print(f"[red]{os.path.basename(install_script)} failed for {repo_short}.[/red]")
            return

    console.print(f"[green]{info.name} updated on Pi.[/green]")


def _run_post_install(console: Console, ssh_client) -> None:
    """Run Pi post-update migrations after any Pi package update."""
    console.print()
    console.print("[bold]Running post-install on Pi...[/bold]")

    try:
        _, stdout, stderr = ssh_client.exec_command(
            "sudo python3 -m raccoon_cli.server.cli post-install", timeout=120
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


BUNDLE_FILE = "/etc/raccoon-bundle.json"
_REMOTE_TMP_BUNDLE = "/tmp/raccoon-bundle.json"
# Restarted so every service re-runs its startup contract check against the new file.
# raccoon-server goes last and without waiting, because it is the one we talk to.
_PI_SERVICES = ["stm32_data_reader.service", "flutter-ui.service"]


def _install_bundle_file(console: Console, ssh_client, manifest: dict) -> bool:
    """Write the bundle manifest to /etc/raccoon-bundle.json on the Pi. True on success."""
    import io
    import json

    console.print()
    console.print("[bold]Writing bundle file on Pi...[/bold]")
    document = json.dumps(manifest, indent=2) + "\n"
    try:
        sftp = ssh_client.open_sftp()
        try:
            sftp.putfo(io.BytesIO(document.encode()), _REMOTE_TMP_BUNDLE)
        finally:
            sftp.close()
        command = (
            f"sudo install -m 644 -o root -g root {_REMOTE_TMP_BUNDLE} {BUNDLE_FILE}"
            f" && rm -f {_REMOTE_TMP_BUNDLE}"
            # status files of the startup checks live here (the image creates it via tmpfiles.d)
            " && sudo install -d -m 1777 /run/raccoon"
        )
        _, stdout, stderr = ssh_client.exec_command(command, timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        err = stderr.read().decode(errors="replace").strip()
    except Exception as exc:
        _error_badge(console, f"Could not write {BUNDLE_FILE} on the Pi: {exc}")
        return False
    if exit_code != 0:
        _error_badge(console, f"Could not write {BUNDLE_FILE} on the Pi (exit {exit_code}): {err}")
        return False
    console.print(f"[green]{BUNDLE_FILE} -> bundle {manifest.get('bundle', '?')}[/green]")
    return True


def _read_bundle_file(ssh_client) -> Optional[dict]:
    """The bundle file on the Pi as a dict, or None when missing or unreadable."""
    import json

    try:
        sftp = ssh_client.open_sftp()
        try:
            with sftp.open(BUNDLE_FILE, "r") as handle:
                data = json.loads(handle.read().decode())
        finally:
            sftp.close()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _mark_components_dev(console: Console, ssh_client, names: list[str], fallback: Optional[dict]) -> bool:
    """Set ``"dev"`` as the version of ``names`` in the Pi's bundle file. True on success.

    Everything else in the file stays. Without a readable file the dev manifest is the
    base, so the file still names the components that were not touched.
    """
    if not names:
        return False
    document = _read_bundle_file(ssh_client)
    if document is None:
        document = {"bundle": "dev", **{k: v for k, v in (fallback or {}).items() if k != "bundle"}}
    components = document.get("components")
    if not isinstance(components, dict):
        components = {}
    for name in names:
        components[name] = "dev"
    document["components"] = components
    return _install_bundle_file(console, ssh_client, document)


def _restart_pi_services(console: Console, ssh_client) -> None:
    """Restart the services so they check their transport against the new bundle file."""
    console.print("[bold]Restarting services on Pi...[/bold]")
    commands = [f"sudo systemctl restart {' '.join(_PI_SERVICES)}", "sudo systemctl --no-block restart raccoon.service"]
    for command in commands:
        try:
            _, stdout, stderr = ssh_client.exec_command(command, timeout=60)
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace").strip()
        except Exception as exc:
            console.print(f"[yellow]`{command}` failed: {exc}[/yellow]")
            continue
        if exit_code != 0:
            console.print(f"[yellow]`{command}` failed (exit {exit_code}): {err}[/yellow]")
    console.print("[green]Services restarted.[/green] Run [cyan]raccoon doctor[/cyan] to see their contract status.")


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
