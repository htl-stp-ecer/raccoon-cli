#!/usr/bin/env python3
"""install.py — Deploy raccoon server to Raspberry Pi from release tarball.

Usage:
    RPI_HOST=192.168.4.1 python install.py

Env vars:
    RPI_HOST  — Pi IP address (default: 192.168.4.1)
    RPI_USER  — Pi SSH user   (default: pi)
"""

import glob
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, raising on failure."""
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"ERROR: Command failed: {' '.join(cmd)}")
        sys.exit(1)
    return result


def ssh(host: str, user: str, command: str, check: bool = True) -> int:
    """Run a command on the Pi via SSH."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", f"{user}@{host}", command],
        capture_output=not check,
    )
    if check and result.returncode != 0:
        print(f"ERROR: SSH command failed: {command}")
        sys.exit(1)
    return result.returncode


def scp(sources: list[str], dest: str) -> None:
    """Copy files to the Pi via SCP."""
    run(["scp", *sources, dest])


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    host = os.environ.get("RPI_HOST", "192.168.4.1")
    user = os.environ.get("RPI_USER", "pi")

    # --- Preflight: check that wheels exist ---
    transport_whls = glob.glob(str(script_dir / "raccoon_transport-*.whl"))
    raccoon_whls = glob.glob(str(script_dir / "raccoon_cli-*.whl"))

    if not transport_whls or not raccoon_whls:
        print(f"ERROR: Expected raccoon_transport-*.whl and raccoon_cli-*.whl in {script_dir}")
        sys.exit(1)

    transport_whl = transport_whls[0]
    raccoon_whl = raccoon_whls[0]

    print(f"Deploying to {user}@{host}")
    print(f"  raccoon-transport: {Path(transport_whl).name}")
    print(f"  raccoon:           {Path(raccoon_whl).name}")

    # --- Test SSH connection ---
    print("Testing SSH connection...")
    if ssh(host, user, "true", check=False) != 0:
        print(f"ERROR: Cannot connect to {user}@{host}")
        sys.exit(1)

    # --- Stop service ---
    print("Stopping raccoon service...")
    ssh(host, user, "sudo systemctl stop raccoon.service 2>/dev/null || true")

    # --- Upload wheels ---
    print("Uploading wheels...")
    remote_tmp = "/tmp/raccoon-install"
    ssh(host, user, f"rm -rf {remote_tmp} && mkdir -p {remote_tmp}")
    scp([transport_whl, raccoon_whl], f"{user}@{host}:{remote_tmp}/")

    # --- Remove stale user-level install that would shadow the system-wide one ---
    print("Removing stale user-level install (if any)...")
    ssh(
        host,
        user,
        "python3 -m pip uninstall raccoon-cli -y --break-system-packages 2>/dev/null || true",
    )

    # --- Install ---
    print("Installing...")
    ssh(
        host,
        user,
        f"sudo pip3 install --break-system-packages --force-reinstall --no-deps "
        f"{remote_tmp}/raccoon_transport-*.whl {remote_tmp}/raccoon_cli-*.whl "
        f"&& sudo pip3 install --break-system-packages {remote_tmp}/raccoon_cli-*.whl",
    )

    # --- Install & start systemd service ---
    print("Configuring systemd service...")
    ssh(host, user, "sudo raccoon-server install")

    # --- Ensure shell completion state exists ---
    ssh(
        host,
        user,
        'if [ ! -f ~/.raccoon/cli_state.yml ]; then '
        "  mkdir -p ~/.raccoon; "
        '  echo "completion_offered: true" > ~/.raccoon/cli_state.yml; '
        "else "
        '  grep -q "^completion_offered:" ~/.raccoon/cli_state.yml '
        '  || echo "completion_offered: true" >> ~/.raccoon/cli_state.yml; '
        "fi",
    )

    # --- Restart service ---
    print("Starting raccoon service...")
    ssh(host, user, "sudo systemctl restart raccoon.service")

    # --- Verify ---
    print()
    ssh(host, user, "systemctl is-active raccoon.service && raccoon-server status", check=False)
    print()
    print(f"Deployment to {host} completed.")


if __name__ == "__main__":
    main()
