"""CLI for raccoon-server management commands."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

SYSTEMD_SERVICE_NAME = "raccoon.service"
SYSTEMD_SERVICE_PATH = Path("/etc/systemd/system") / SYSTEMD_SERVICE_NAME

_NETWORK_SERVICES = ["wifi-power-save-off.service", "gratuitous-arp.service"]
_SYSTEMD_DIR = Path("/etc/systemd/system")
_PACKAGE_SYSTEMD = Path(__file__).parent / "systemd"


def _install_network_services() -> list[str]:
    """Install Wi-Fi fix services. Returns list of installed service names."""
    installed = []
    for name in _NETWORK_SERVICES:
        src = _PACKAGE_SYSTEMD / name
        if not src.exists():
            continue
        dst = _SYSTEMD_DIR / name
        shutil.copy(src, dst)
        subprocess.run(["systemctl", "enable", "--now", name], check=False)
        installed.append(name)
    return installed


@click.group()
def main():
    """Raccoon Server - management commands for the Pi-side daemon."""
    pass


@main.command()
def start():
    """Start the Raccoon server (foreground mode)."""
    from raccoon_cli.server.config import load_config

    config = load_config()

    console.print(f"[green]Starting Raccoon Server on port {config.port}...[/green]")
    console.print(f"Projects directory: {config.projects_dir}")
    console.print()

    import uvicorn

    uvicorn.run(
        "raccoon_cli.server.app:app",
        host=config.host,
        port=config.port,
        reload=False,
        log_level="info",
    )


@main.command()
@click.option("--user", default="pi", help="User to run the service as")
def install(user: str):
    """Install the Raccoon server as a systemd service."""
    # Check if running as root
    if os.geteuid() != 0:
        console.print("[red]Error: This command must be run as root (sudo)[/red]")
        sys.exit(1)

    # Find the bundled service file (inside raccoon package)
    package_dir = Path(__file__).parent.parent
    source_service = package_dir / "systemd" / SYSTEMD_SERVICE_NAME

    if not source_service.exists():
        # Create service file content directly
        service_content = f"""[Unit]
Description=Raccoon Robotics Toolchain Server
After=network.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory=/home/{user}

Environment=RACCOON_PROJECTS_DIR=/home/{user}/programs
Environment=RACCOON_PORT=8421
Environment=RACCOON_HOST=0.0.0.0

ExecStart=/usr/bin/python3 -m raccoon_cli.server

Restart=always
RestartSec=3
MemoryMax=256M

StandardOutput=journal
StandardError=journal
SyslogIdentifier=raccoon

[Install]
WantedBy=multi-user.target
"""
        SYSTEMD_SERVICE_PATH.write_text(service_content)
    else:
        # Copy bundled service file
        shutil.copy(source_service, SYSTEMD_SERVICE_PATH)

    # Create projects directory
    projects_dir = Path(f"/home/{user}/programs")
    projects_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", f"{user}:{user}", str(projects_dir)])

    # Reload systemd and enable service
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "raccoon"], check=True)
    subprocess.run(["systemctl", "start", "raccoon"], check=True)

    # Install network fix services
    installed = _install_network_services()
    if installed:
        subprocess.run(["systemctl", "daemon-reload"], check=True)

    console.print("[green]Raccoon server installed and started![/green]")
    console.print()
    console.print("Service status:")
    subprocess.run(["systemctl", "status", "raccoon", "--no-pager", "-l"])


@main.command()
def uninstall():
    """Uninstall the Raccoon server systemd service."""
    if os.geteuid() != 0:
        console.print("[red]Error: This command must be run as root (sudo)[/red]")
        sys.exit(1)

    # Stop and disable service
    subprocess.run(["systemctl", "stop", "raccoon"], check=False)
    subprocess.run(["systemctl", "disable", "raccoon"], check=False)

    # Remove service file
    if SYSTEMD_SERVICE_PATH.exists():
        SYSTEMD_SERVICE_PATH.unlink()

    subprocess.run(["systemctl", "daemon-reload"], check=True)

    console.print("[green]Raccoon server uninstalled.[/green]")


@main.command()
def status():
    """Show the status of the Raccoon server service."""
    result = subprocess.run(
        ["systemctl", "is-active", "raccoon"], capture_output=True, text=True
    )

    if result.stdout.strip() == "active":
        console.print("[green]Raccoon server is running[/green]")
        subprocess.run(["systemctl", "status", "raccoon", "--no-pager", "-l"])
    else:
        console.print("[yellow]Raccoon server is not running[/yellow]")
        console.print("Start with: [cyan]sudo systemctl start raccoon[/cyan]")


@main.command()
def logs():
    """Show recent logs from the Raccoon server."""
    subprocess.run(["journalctl", "-u", "raccoon", "-n", "50", "--no-pager"])


@main.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
def tail(follow: bool):
    """Tail the Raccoon server logs."""
    cmd = ["journalctl", "-u", "raccoon"]
    if follow:
        cmd.append("-f")
    else:
        cmd.extend(["-n", "20"])
    subprocess.run(cmd)


@main.command()
def restart():
    """Restart the Raccoon server service."""
    if os.geteuid() != 0:
        console.print("[red]Error: This command must be run as root (sudo)[/red]")
        sys.exit(1)

    subprocess.run(["systemctl", "restart", "raccoon"], check=True)
    console.print("[green]Raccoon server restarted[/green]")


@main.command()
def config():
    """Show the current server configuration."""
    from raccoon_cli.server.config import load_config

    cfg = load_config()
    console.print("[bold]Raccoon Server Configuration[/bold]")
    console.print()
    for key, value in cfg.to_dict().items():
        console.print(f"  {key}: [cyan]{value}[/cyan]")


if __name__ == "__main__":
    main()
