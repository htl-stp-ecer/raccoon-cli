"""Shell command - open an SSH session to the connected Pi."""

import signal
import subprocess

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

    # Do not use os.execvp: on Windows it spawns ssh and exits the parent
    # immediately, so the parent shell redraws its prompt and both fight over
    # stdin. Run ssh as a child, wait for it, and forward its exit code.
    try:
        proc = subprocess.Popen(["ssh", f"{user}@{address}"])
    except FileNotFoundError:
        console.print("[red]ssh not found on PATH.[/red]")
        raise SystemExit(1)

    def _forward(signum, frame):
        proc.terminate()

    signal.signal(signal.SIGTERM, _forward)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _forward)

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        # SIGTERM (not SIGKILL) lets ssh restore the terminal before exiting.
        proc.terminate()
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()

    raise SystemExit(rc if rc >= 0 else 128 - rc)
