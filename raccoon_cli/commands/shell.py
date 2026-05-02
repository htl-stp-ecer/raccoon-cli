"""Shell command - open an SSH session to the connected Pi."""

import os

import click
from rich.console import Console

from raccoon_cli.client.connection import get_connection_manager
from raccoon_cli.project import find_project_root, load_project_config


def _resolve_ssh_target() -> tuple[str, str] | None:
    """Return (user, address) from session state or project/global config."""
    manager = get_connection_manager()

    if manager.is_connected:
        s = manager.state
        return s.pi_user, s.pi_address

    # Try project YAML (supports !include / !include-merge)
    project_root = find_project_root()
    if project_root:
        try:
            config = load_project_config(project_root)
            conn = config.get("connection", {})
            address = conn.get("pi_address")
            if address:
                return conn.get("pi_user", "pi"), address
        except Exception:
            pass

    # Fall back to first known Pi from global config
    known = manager.load_known_pis()
    if known:
        pi = known[0]
        address = pi.get("address")
        if address:
            return pi.get("user", "pi"), address

    return None


@click.command(name="shell")
@click.pass_context
def shell_command(ctx: click.Context) -> None:
    """Open an interactive SSH shell on the connected Pi."""
    console: Console = ctx.obj.get("console", Console())

    target = _resolve_ssh_target()
    if not target:
        console.print(
            "[red]No Pi address found. Run [cyan]raccoon connect <address>[/cyan] first.[/red]"
        )
        raise SystemExit(1)

    user, address = target
    os.execvp("ssh", ["ssh", f"{user}@{address}"])
