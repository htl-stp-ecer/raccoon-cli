#!/usr/bin/env python3
"""deploy.py — install a local raccoon-cli wheel on a Pi (for development).

Copies the wheel from this machine to the Pi and installs it there. Releases are
installed by `raccoon update` (from PyPI), not by this script.

Usage:
    python3 deploy.py --host 192.168.4.1                          # the wheel from the last build.py run
    python3 deploy.py --host 192.168.4.1 --artifact my.whl        # a specific wheel

raccoon-cli is set to "dev" in the Pi's /etc/raccoon-bundle.json: a deployed wheel
is a custom build.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

COMPONENT = "raccoon-cli"
BUNDLE_FILE = "/etc/raccoon-bundle.json"
REMOTE_TMP = "/tmp/raccoon-install"

# Runs on the Pi as root: set this component's version to "dev" in the bundle file and
# leave everything else in it as it is.
MARK_DEV = r"""
import json, os, tempfile
path = "%(path)s"
try:
    with open(path) as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        document = {}
except (OSError, ValueError):
    document = {}
document.setdefault("bundle", "dev")
components = document.get("components")
if not isinstance(components, dict):
    components = {}
components["%(component)s"] = "dev"
document["components"] = components
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".raccoon-bundle.")
with os.fdopen(fd, "w") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
os.makedirs("/run/raccoon", exist_ok=True)
os.chmod("/run/raccoon", 0o1777)
"""


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


class Pi:
    def __init__(self, host: str, user: str, ssh_params: str) -> None:
        self.target = f"{user}@{host}"
        self.params = shlex.split(ssh_params)

    def run(self, command: str, *, check: bool = True, stdin: str | None = None) -> int:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", *self.params, self.target, command],
            input=stdin,
            text=True,
        )
        if check and result.returncode != 0:
            fail(f"command failed on the Pi: {command}")
        return result.returncode

    def copy(self, local: Path, remote_dir: str) -> None:
        subprocess.run(["scp", *self.params, str(local), f"{self.target}:{remote_dir}/"], check=True)


def find_wheel(artifact: str | None) -> Path:
    if artifact:
        path = Path(artifact)
    else:
        manifest = Path(__file__).resolve().parent / "builds" / "manifest.json"
        if not manifest.is_file():
            fail("no builds/manifest.json. Run build.py first, or pass --artifact <wheel>.")
        files = json.loads(manifest.read_text())["files"]
        wheels = [f["name"] for f in files if f.get("kind") == "wheel"]
        if len(wheels) != 1:
            fail(f"expected one wheel in {manifest}, found {len(wheels)}")
        path = manifest.parent / wheels[0]
    if not path.is_file() or not (path.name.startswith("raccoon_cli-") and path.suffix == ".whl"):
        fail(f"not a raccoon_cli wheel: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a raccoon-cli wheel on a Pi.")
    parser.add_argument("--host", required=True, help="Pi address")
    parser.add_argument("--user", default="pi", help="SSH user (default: pi)")
    parser.add_argument("--artifact", help="wheel to install (default: the one in builds/manifest.json)")
    parser.add_argument("--ssh-params", default="", help='extra ssh/scp options, e.g. "-o StrictHostKeyChecking=no"')
    args = parser.parse_args()

    wheel = find_wheel(args.artifact)
    pi = Pi(args.host, args.user, args.ssh_params)
    print(f"Deploying {wheel.name} to {pi.target}")

    print("Testing SSH connection...")
    if pi.run("true", check=False) != 0:
        fail(f"cannot connect to {pi.target} (see the ssh message above). "
             f"Host key changed (e.g. after flashing a new image)?  ssh-keygen -R {args.host}")

    print("Stopping raccoon service...")
    pi.run("sudo systemctl stop raccoon.service 2>/dev/null || true")

    print("Uploading wheel...")
    pi.run(f"rm -rf {REMOTE_TMP} && mkdir -p {REMOTE_TMP}")
    pi.copy(wheel, REMOTE_TMP)
    remote_wheel = f"{REMOTE_TMP}/{wheel.name}"

    print("Installing...")
    # A user-level install would shadow the system-wide one.
    pi.run("python3 -m pip uninstall raccoon-cli -y --break-system-packages 2>/dev/null || true", check=False)
    pi.run(f"sudo pip3 install --break-system-packages --force-reinstall --no-deps {shlex.quote(remote_wheel)}")
    # raccoon-transport comes from PyPI, not from the wheel.
    pi.run(f"sudo pip3 install --break-system-packages --upgrade raccoon-transport {shlex.quote(remote_wheel)}")

    # The bundle file must be right before the service starts: it checks itself against it.
    print(f'Marking {COMPONENT} as "dev" in {BUNDLE_FILE}...')
    pi.run("sudo python3 -", stdin=MARK_DEV % {"path": BUNDLE_FILE, "component": COMPONENT})

    print("Configuring systemd service and running migrations...")
    pi.run("sudo python3 -m raccoon_cli.server.cli post-install")

    print()
    pi.run("systemctl is-active raccoon.service && python3 -m raccoon_cli.server.cli status", check=False)
    print()
    print(f"Deployment to {args.host} completed.")


if __name__ == "__main__":
    main()
