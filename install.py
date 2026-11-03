#!/usr/bin/env python3
"""install.py — Deploy raccoon server to Raspberry Pi from release tarball.

Usage:
    RPI_HOST=192.168.4.1 python install.py

Env vars:
    RPI_HOST  — Pi IP address (default: 192.168.4.1)
    RPI_USER  — Pi SSH user   (default: pi)
    RPI_SSH_OPTS — extra ssh/scp options, e.g. "-o StrictHostKeyChecking=no"
                   (``raccoon update`` sets this to match its own SSH policy)
"""

import glob
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def ensure_uv() -> None:
    """Install uv locally if not already available."""
    if shutil.which("uv"):
        return
    print("uv not found — installing via pip...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "uv"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Failed to install uv:\n{result.stderr}")
        sys.exit(1)
    if not shutil.which("uv"):
        print("WARNING: uv installed but not found in PATH — you may need to restart your shell.")
    else:
        print("uv installed successfully.")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, raising on failure."""
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"ERROR: Command failed: {' '.join(cmd)}")
        sys.exit(1)
    return result


def _ssh_opts() -> list[str]:
    return shlex.split(os.environ.get("RPI_SSH_OPTS", ""))


def ssh(host: str, user: str, command: str, check: bool = True) -> int:
    """Run a command on the Pi via SSH."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", *_ssh_opts(), f"{user}@{host}", command],
    )
    if check and result.returncode != 0:
        print(f"ERROR: SSH command failed: {command}")
        sys.exit(1)
    return result.returncode


def scp(sources: list[str], dest: str) -> None:
    """Copy files to the Pi via SCP."""
    run(["scp", *_ssh_opts(), *sources, dest])


def main() -> None:
    ensure_uv()

    script_dir = Path(__file__).resolve().parent
    host = os.environ.get("RPI_HOST", "192.168.4.1")
    user = os.environ.get("RPI_USER", "pi")

    # --- Preflight: check that the wheel exists ---
    raccoon_whls = glob.glob(str(script_dir / "raccoon_cli-*.whl"))

    if not raccoon_whls:
        print(f"ERROR: Expected raccoon_cli-*.whl in {script_dir}")
        sys.exit(1)

    raccoon_whl = raccoon_whls[0]

    print(f"Deploying to {user}@{host}")
    print(f"  raccoon: {Path(raccoon_whl).name}")

    # --- Test SSH connection ---
    print("Testing SSH connection...")
    if ssh(host, user, "true", check=False) != 0:
        print(f"ERROR: Cannot connect to {user}@{host} (see the ssh message above).")
        print(f"  Host key changed (e.g. after flashing a new image)?  ssh-keygen -R {host}")
        sys.exit(1)

    # --- Stop service ---
    print("Stopping raccoon service...")
    ssh(host, user, "sudo systemctl stop raccoon.service 2>/dev/null || true")

    # --- Upload wheel ---
    print("Uploading wheel...")
    remote_tmp = "/tmp/raccoon-install"
    ssh(host, user, f"rm -rf {remote_tmp} && mkdir -p {remote_tmp}")
    scp([raccoon_whl], f"{user}@{host}:{remote_tmp}/")

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
        f"{remote_tmp}/raccoon_cli-*.whl "
        # raccoon-transport comes from PyPI, not from this release.
        f"&& sudo pip3 install --break-system-packages --upgrade raccoon-transport "
        f"{remote_tmp}/raccoon_cli-*.whl",
    )

    # --- Install systemd service + run all migrations ---
    print("Configuring systemd service and running migrations...")
    ssh(host, user, "sudo python3 -m raccoon_cli.server.cli post-install")

    # --- Verify ---
    print()
    ssh(
        host,
        user,
        "systemctl is-active raccoon.service && python3 -m raccoon_cli.server.cli status",
        check=False,
    )
    print()
    print(f"Deployment to {host} completed.")


if __name__ == "__main__":
    main()
