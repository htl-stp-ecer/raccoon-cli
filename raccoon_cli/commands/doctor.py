"""Doctor command - system health check (tools, connection, versions)."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from dataclasses import dataclass

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


# ---------------------------------------------------------------------------
# Tool checks
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    description: str
    required: bool
    fix_hint: str
    ok: bool = False
    detail: str = ""


def _check_binary(name: str, description: str, required: bool, fix_hint: str) -> Check:
    found = shutil.which(name)
    c = Check(name=name, description=description, required=required, fix_hint=fix_hint)
    c.ok = found is not None
    c.detail = found or ""
    return c


def _check_python_package(
    import_name: str,
    display_name: str,
    description: str,
    required: bool,
    fix_hint: str,
) -> Check:
    c = Check(name=display_name, description=description, required=required, fix_hint=fix_hint)
    spec = importlib.util.find_spec(import_name)
    c.ok = spec is not None
    c.detail = spec.origin if (spec and spec.origin) else ""
    return c


def _iter_checks():
    yield _check_binary(
        "ssh", "SSH client (raccoon shell, key setup)",
        required=True,
        fix_hint="sudo apt install openssh-client",
    )
    yield _check_binary(
        "git", "Git (project version tracking)",
        required=True,
        fix_hint="sudo apt install git  /  brew install git",
    )
    yield _check_python_package(
        "paramiko", "paramiko",
        "SSH/SFTP library (connect, sync)",
        required=True,
        fix_hint="pip install paramiko",
    )
    yield _check_python_package(
        "black", "black",
        "Code formatter (codegen output)",
        required=True,
        fix_hint="pip install black",
    )
    yield _check_binary(
        "rsync", "rsync (faster sync, falls back to SFTP)",
        required=False,
        fix_hint="sudo apt install rsync  /  brew install rsync",
    )
    yield _check_binary(
        "uv", "uv (fast package manager, local project runs)",
        required=False,
        fix_hint="pip install uv",
    )

    pycharm_path = shutil.which("pycharm") or shutil.which("pycharm-community") or shutil.which("pycharm.sh")
    c = Check(
        name="pycharm",
        description="PyCharm IDE (raccoon open)",
        required=False,
        fix_hint="jetbrains.com/pycharm",
    )
    c.ok = pycharm_path is not None
    c.detail = pycharm_path or ""
    yield c


def stream_tool_checks(console: Console) -> bool:
    """Print each tool check as it runs. Returns True if all required checks pass."""
    all_ok = True
    for c in _iter_checks():
        if c.ok:
            icon = "[green]✓[/green]"
            note = f"[dim]{c.detail or 'found'}[/dim]"
        elif c.required:
            icon = "[red]✗[/red]"
            note = f"[red]MISSING[/red] [dim]— {c.fix_hint}[/dim]"
            all_ok = False
        else:
            icon = "[yellow]−[/yellow]"
            note = f"[yellow]not found[/yellow] [dim]— {c.fix_hint}[/dim]"

        label = f"[bold]{c.name}[/bold]"
        req_tag = "" if c.required else " [dim](optional)[/dim]"
        console.print(f"  {icon}  {label}{req_tag}  {note}")

    return all_ok


# ---------------------------------------------------------------------------
# Connection / project / version sections (absorbed from status)
# ---------------------------------------------------------------------------

def _try_auto_connect(console: Console, manager, project_root) -> None:
    from raccoon_cli.client.connection import check_paramiko_version, ParamikoVersionError
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


def _show_connection(console: Console, manager) -> None:
    from raccoon_cli.project import find_project_root, load_project_config
    from raccoon_cli.client.api import create_api_client

    state = manager.state
    project_root = find_project_root()

    if state.connected:
        auth_status = "[green]authenticated[/green]" if state.api_token else "[yellow]no token[/yellow]"
        console.print(
            Panel(
                f"[green]Connected[/green] to [cyan]{state.pi_hostname}[/cyan]\n"
                f"Address: {state.pi_address}:{state.pi_port}\n"
                f"User:    {state.pi_user}\n"
                f"Version: {state.pi_version or 'unknown'}\n"
                f"Auth:    {auth_status}",
                title="Pi Connection",
            )
        )
    else:
        console.print(
            Panel(
                "[yellow]Not connected[/yellow]\n"
                "Use [cyan]raccoon connect <address>[/cyan] to connect to a Pi",
                title="Pi Connection",
            )
        )

    if project_root:
        try:
            config = load_project_config(project_root)
            project_name = config.get("name", project_root.name)
            project_uuid = config.get("uuid", "unknown")
            saved_pi = config.get("connection", {}).get("pi_address")

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

            if state.connected:
                console.print()
                asyncio.run(_show_remote_status(console, state, project_uuid))
        except Exception as e:
            console.print(f"[red]Error loading project config: {e}[/red]")
    else:
        console.print()
        console.print("[dim]Not in a Raccoon project directory[/dim]")

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
                (pi.get("last_seen", "unknown") or "unknown")[:10],
            )
        console.print(table)


async def _show_remote_status(console: Console, state, project_uuid: str) -> None:
    from raccoon_cli.client.api import create_api_client
    try:
        async with create_api_client(state.pi_address, state.pi_port, api_token=state.api_token) as client:
            health = await client.health()
            projects_dir = health.get("projects_dir", "unknown")
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


def _show_package_versions(console: Console, manager) -> None:
    from raccoon_cli.version_checker import check_all_versions, render_version_table

    ssh_client = None
    server_url = None
    api_token = None
    if manager.is_connected:
        try:
            ssh_client = manager.get_ssh_client()
        except Exception:
            pass
        if manager.state.pi_address:
            server_url = f"http://{manager.state.pi_address}:{manager.state.pi_port}"
            api_token = manager.state.api_token

    console.print()
    statuses = check_all_versions(ssh_client=ssh_client, server_url=server_url, api_token=api_token)
    any_outdated = render_version_table(console, statuses)
    if any_outdated:
        console.print()
        console.print("Run [cyan]raccoon update[/cyan] to install updates.")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

@click.command(name="doctor")
@click.pass_context
def doctor_command(ctx: click.Context) -> None:
    """Show system health: connection, tools, and package versions."""
    from raccoon_cli.client.connection import get_connection_manager
    from raccoon_cli.project import find_project_root

    console: Console = ctx.obj.get("console", Console())
    manager = get_connection_manager()
    project_root = find_project_root()

    if not manager.is_connected:
        _try_auto_connect(console, manager, project_root)

    _show_connection(console, manager)

    _show_package_versions(console, manager)

    console.print()
    console.print("[bold]External tools[/bold]")
    all_ok = stream_tool_checks(console)
    console.print()

    if not all_ok:
        console.print("[red]✗ One or more required tools are missing.[/red]")
        raise SystemExit(1)
