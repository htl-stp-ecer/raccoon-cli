"""Shared version checking logic for raccoon update and status commands."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.table import Table

logger = logging.getLogger("raccoon")

INSTALLED_VERSIONS_PATH = ".raccoon/installed_versions.yml"


@dataclass
class PackageInfo:
    """Metadata about a trackable package."""

    name: str
    repo: str
    pip_name: Optional[str]
    targets: list[str]
    # SSH command that exits 0 if the package is installed on Pi.
    # Used as fallback when pip_name is None and tracked versions are missing.
    detect_cmd: Optional[str] = None


PACKAGE_REGISTRY: list[PackageInfo] = [
    PackageInfo(
        name="raccoon",
        repo="htl-stp-ecer/raccoon-cli",
        pip_name="raccoon",
        targets=["laptop", "pi"],
    ),
    PackageInfo(
        name="raccoon-transport",
        repo="htl-stp-ecer/raccoon-cli",
        pip_name="raccoon-transport",
        targets=["pi"],
    ),
    PackageInfo(
        name="libstp",
        repo="htl-stp-ecer/raccoon-lib",
        pip_name="libstp",
        targets=["pi"],
    ),
    PackageInfo(
        name="botui",
        repo="htl-stp-ecer/botui",
        pip_name=None,
        targets=["pi"],
        detect_cmd="systemctl cat botui.service >/dev/null 2>&1 || test -d /opt/botui",
    ),
    PackageInfo(
        name="stm32-data-reader",
        repo="htl-stp-ecer/stm32-data-reader",
        pip_name=None,
        targets=["pi"],
        detect_cmd="systemctl cat stm32_data_reader.service >/dev/null 2>&1",
    ),
]


@dataclass
class PackageStatus:
    """Version status for a single package."""

    info: PackageInfo
    latest_version: Optional[str] = None
    laptop_version: Optional[str] = None
    pi_version: Optional[str] = None


def check_gh_available() -> bool:
    """Check if the GitHub CLI is available."""
    return shutil.which("gh") is not None


def get_latest_version(repo: str) -> Optional[str]:
    """Get the latest release version for a GitHub repo using `gh`."""
    try:
        result = subprocess.run(
            ["gh", "release", "view", "--repo", repo, "--json", "tagName", "-q", ".tagName"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            tag = result.stdout.strip()
            return tag.lstrip("v") if tag else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def download_release_assets(
    repo: str, pattern: str, dest_dir: str, tag: str = "latest"
) -> list[str]:
    """Download release assets from a GitHub repo using the gh API.

    Uses ``gh api`` with ``Accept: application/octet-stream`` which works
    reliably for both public and private repos (``gh release download``
    silently produces empty files on some private-repo configurations).

    Args:
        repo: GitHub repo in ``owner/repo`` format.
        pattern: fnmatch glob matched against asset filenames.
        dest_dir: Directory to write downloaded files into.
        tag: Release tag to download from, or ``"latest"``.

    Returns:
        List of downloaded file paths.
    """
    # Resolve release assets via the API
    if tag == "latest":
        api_path = f"repos/{repo}/releases/latest"
    else:
        api_path = f"repos/{repo}/releases/tags/{tag}"

    try:
        result = subprocess.run(
            ["gh", "api", api_path, "-q", ".assets[] | .name + \"\\t\" + .url"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Failed to list assets for %s: %s", repo, result.stderr.strip())
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    downloaded: list[str] = []
    for line in result.stdout.strip().splitlines():
        if "\t" not in line:
            continue
        name, api_url = line.split("\t", 1)
        if not fnmatch.fnmatch(name, pattern):
            continue

        dest_path = os.path.join(dest_dir, name)
        try:
            dl = subprocess.run(
                ["gh", "api", api_url, "-H", "Accept: application/octet-stream"],
                capture_output=True,
                timeout=120,
            )
            if dl.returncode == 0 and dl.stdout:
                with open(dest_path, "wb") as f:
                    f.write(dl.stdout)
                downloaded.append(dest_path)
            else:
                logger.warning("Failed to download %s", name)
        except subprocess.TimeoutExpired:
            logger.warning("Timeout downloading %s", name)

    return downloaded


def _is_editable_install(pip_name: str) -> bool:
    """Check if a pip package is an editable (dev) install via direct_url.json."""
    try:
        result = subprocess.run(
            ["pip", "show", "-f", pip_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        # Find the install location
        location = None
        pkg_name = None
        version = None
        for line in result.stdout.splitlines():
            if line.startswith("Location:"):
                location = line.split(":", 1)[1].strip()
            elif line.startswith("Name:"):
                pkg_name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
        if not location or not pkg_name or not version:
            return False
        # Check for direct_url.json in dist-info
        dist_info = Path(location) / f"{pkg_name.replace('-', '_')}-{version}.dist-info" / "direct_url.json"
        if dist_info.exists():
            data = json.loads(dist_info.read_text())
            url = data.get("url", "")
            return url.startswith("file://")
    except Exception:
        pass
    return False


def get_local_pip_version(pip_name: str) -> Optional[str]:
    """Get installed version of a local pip package.

    Returns "dev" for editable/local installs.
    """
    try:
        result = subprocess.run(
            ["pip", "show", pip_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                    if _is_editable_install(pip_name):
                        return "dev"
                    return version
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_remote_pip_version(ssh_client, pip_name: str) -> Optional[str]:
    """Get installed version of a pip package on the Pi via SSH.

    Tries both user and system-level pip since packages may be
    installed with sudo.
    """
    for cmd in [f"pip3 show {pip_name}", f"sudo pip3 show {pip_name}"]:
        try:
            _, stdout, stderr = ssh_client.exec_command(cmd, timeout=10)
            output = stdout.read().decode()
            for line in output.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return None


def get_remote_tracked_versions(ssh_client) -> dict[str, str]:
    """Read all tracked versions from ~/.raccoon/installed_versions.yml on Pi."""
    try:
        _, stdout, _ = ssh_client.exec_command(
            f"cat ~/{INSTALLED_VERSIONS_PATH}", timeout=10
        )
        content = stdout.read().decode()
        if content.strip():
            data = yaml.safe_load(content)
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def write_remote_tracked_version(
    ssh_client, name: str, version: str
) -> None:
    """Write/update a tracked version in ~/.raccoon/installed_versions.yml on Pi."""
    existing = get_remote_tracked_versions(ssh_client)
    existing[name] = version
    yml_content = yaml.safe_dump(existing, default_flow_style=False)
    ssh_client.exec_command(
        f"mkdir -p ~/.raccoon && cat > ~/{INSTALLED_VERSIONS_PATH} << 'EOFVERSIONS'\n{yml_content}EOFVERSIONS"
    )


def _detect_remote_package(ssh_client, pkg: PackageInfo) -> bool:
    """Check if a non-pip package is installed on Pi using its detect_cmd."""
    if not pkg.detect_cmd:
        return False
    try:
        _, stdout, _ = ssh_client.exec_command(pkg.detect_cmd, timeout=10)
        return stdout.channel.recv_exit_status() == 0
    except Exception:
        return False


def check_all_versions(ssh_client=None) -> list[PackageStatus]:
    """Aggregate version info for all packages in the registry.

    Fetches latest versions from GitHub and installed versions from local pip
    and (if ssh_client provided) the Pi.
    """
    if not check_gh_available():
        logger.warning("gh CLI not found — cannot check latest versions")

    # Deduplicate repo lookups (raccoon + raccoon-transport share a repo)
    latest_cache: dict[str, Optional[str]] = {}
    tracked_versions: dict[str, str] = {}

    if ssh_client:
        tracked_versions = get_remote_tracked_versions(ssh_client)

    statuses: list[PackageStatus] = []
    for pkg in PACKAGE_REGISTRY:
        status = PackageStatus(info=pkg)

        # Latest version
        if pkg.repo not in latest_cache:
            latest_cache[pkg.repo] = (
                get_latest_version(pkg.repo) if check_gh_available() else None
            )
        status.latest_version = latest_cache[pkg.repo]

        # Laptop version (pip packages only)
        if "laptop" in pkg.targets and pkg.pip_name:
            status.laptop_version = get_local_pip_version(pkg.pip_name)

        # Pi version
        if "pi" in pkg.targets and ssh_client:
            if pkg.pip_name:
                status.pi_version = get_remote_pip_version(ssh_client, pkg.pip_name)
            else:
                # Non-pip: check tracked versions, then detect presence
                tracked = tracked_versions.get(pkg.name)
                if tracked:
                    status.pi_version = tracked
                elif _detect_remote_package(ssh_client, pkg):
                    status.pi_version = "installed"

        statuses.append(status)

    return statuses


_NON_VERSION_TAGS = {"dev", "installed"}


def _version_style(installed: Optional[str], latest: Optional[str]) -> tuple[str, str]:
    """Return (display_text, style) for a version cell."""
    if installed is None:
        return "—", "dim"
    if installed in _NON_VERSION_TAGS:
        return installed, "blue"
    if latest is None:
        return installed, "dim"
    if installed == latest:
        return installed, "green"
    return installed, "yellow"


def render_version_table(console: Console, statuses: list[PackageStatus]) -> bool:
    """Render a Rich table showing package version status.

    Returns True if any package is outdated.
    """
    table = Table(title="Package Versions")
    table.add_column("Package", style="cyan")
    table.add_column("Latest", style="dim")
    table.add_column("Laptop", justify="center")
    table.add_column("Pi", justify="center")

    any_outdated = False

    for s in statuses:
        latest_display = s.latest_version or "?"

        # Laptop column
        if "laptop" in s.info.targets:
            laptop_text, laptop_style = _version_style(s.laptop_version, s.latest_version)
            laptop_cell = f"[{laptop_style}]{laptop_text}[/{laptop_style}]"
            if (
                s.laptop_version
                and s.laptop_version not in _NON_VERSION_TAGS
                and s.latest_version
                and s.laptop_version != s.latest_version
            ):
                any_outdated = True
        else:
            laptop_cell = "[dim]n/a[/dim]"

        # Pi column
        if "pi" in s.info.targets:
            pi_text, pi_style = _version_style(s.pi_version, s.latest_version)
            pi_cell = f"[{pi_style}]{pi_text}[/{pi_style}]"
            if (
                s.pi_version
                and s.pi_version not in _NON_VERSION_TAGS
                and s.latest_version
                and s.pi_version != s.latest_version
            ):
                any_outdated = True
        else:
            pi_cell = "[dim]n/a[/dim]"

        table.add_row(s.info.name, latest_display, laptop_cell, pi_cell)

    console.print(table)
    return any_outdated
