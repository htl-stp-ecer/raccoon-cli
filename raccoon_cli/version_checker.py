"""Shared version checking logic for raccoon update and status commands."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.table import Table

logger = logging.getLogger("raccoon")

GITHUB_API = "https://api.github.com"
RACCOON_IMAGE_REPO = "htl-stp-ecer/raccoon-image"


@dataclass
class PackageInfo:
    """Metadata about a trackable package."""

    name: str
    repo: str
    pip_name: Optional[str]
    targets: list[str]
    # SSH command whose stdout is the installed version string.
    # Used when pip_name is None and the server endpoint is unavailable.
    version_cmd: Optional[str] = None
    # True if the package is installable from PyPI by ``pip_name``. If False,
    # updates fall back to downloading the wheel from the GitHub release.
    on_pypi: bool = True


PACKAGE_REGISTRY: list[PackageInfo] = [
    PackageInfo(
        name="raccoon-cli",
        repo="htl-stp-ecer/raccoon-cli",
        pip_name="raccoon-cli",
        targets=["laptop", "pi"],
    ),
    PackageInfo(
        name="raccoon-transport",
        repo="htl-stp-ecer/raccoon-transport",
        pip_name="raccoon-transport",
        targets=["pi"],
    ),
    PackageInfo(
        name="raccoon-lib",
        repo="htl-stp-ecer/raccoon-lib",
        pip_name="raccoon",
        targets=["pi"],
        on_pypi=False,
    ),
    PackageInfo(
        name="raccoon-stubs",
        repo="htl-stp-ecer/raccoon-lib",
        pip_name="raccoon-stubs",
        targets=["laptop"],
    ),
    PackageInfo(
        name="botui",
        repo="htl-stp-ecer/botui",
        pip_name=None,
        targets=["pi"],
        version_cmd="cat /home/pi/stp-velox/version 2>/dev/null",
    ),
    PackageInfo(
        name="stm32-data-reader",
        repo="htl-stp-ecer/stm32-data-reader",
        pip_name=None,
        targets=["pi"],
        version_cmd="/home/pi/stm32_data_reader/stm32_data_reader --version 2>/dev/null",
    ),
    PackageInfo(
        name="raccoon-cam",
        repo="htl-stp-ecer/raccoon-cam",
        pip_name=None,
        targets=["pi"],
        version_cmd="/usr/local/bin/raccoon-cam --version 2>/dev/null",
    ),
]


@dataclass
class PackageStatus:
    """Version status for a single package."""

    info: PackageInfo
    latest_version: Optional[str] = None
    laptop_version: Optional[str] = None
    pi_version: Optional[str] = None


def _github_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "raccoon-cli",
    }


def _fetch_release(repo: str, tag: str = "latest") -> Optional[dict]:
    """Fetch a release JSON payload from the public GitHub API."""
    if tag == "latest":
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    else:
        url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    try:
        resp = httpx.get(url, headers=_github_headers(), timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("GitHub API %s returned %s", url, resp.status_code)
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch %s: %s", url, e)
    return None


def get_latest_version(repo: str) -> Optional[str]:
    """Get the latest release version for a public GitHub repo."""
    data = _fetch_release(repo, "latest")
    if not data:
        return None
    tag = data.get("tag_name") or ""
    return tag.lstrip("v") if tag else None


def download_release_assets(
    repo: str, pattern: str, dest_dir: str, tag: str = "latest"
) -> list[str]:
    """Download release assets from a public GitHub repo via the REST API.

    Args:
        repo: GitHub repo in ``owner/repo`` format.
        pattern: fnmatch glob matched against asset filenames.
        dest_dir: Directory to write downloaded files into.
        tag: Release tag to download from, or ``"latest"``.

    Returns:
        List of downloaded file paths.
    """
    data = _fetch_release(repo, tag)
    if not data:
        return []

    downloaded: list[str] = []
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "raccoon-cli"}) as client:
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if not name or not fnmatch.fnmatch(name, pattern):
                continue
            url = asset.get("browser_download_url")
            if not url:
                continue
            dest_path = os.path.join(dest_dir, name)
            try:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        logger.warning("Failed to download %s: HTTP %s", name, resp.status_code)
                        continue
                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
                downloaded.append(dest_path)
            except httpx.HTTPError as e:
                logger.warning("Failed to download %s: %s", name, e)

    return downloaded


_RACCOON_IMAGE_RAW = "https://raw.githubusercontent.com/htl-stp-ecer/raccoon-image/main/bundles"


def fetch_bundle_manifest(bundle: str = "latest") -> Optional[dict]:
    """Fetch a bundle manifest from raccoon-image.

    ``bundle`` can be:
    - ``"latest"``      — pointer to current stable bundle (follows ref)
    - ``"dev"``         — latest component tips, auto-updated by CI
    - ``"2026.4.25.1"`` — a specific hand-curated bundle file

    If the fetched file contains only a ``ref`` key, the ref is followed
    once to load the actual bundle.
    """
    url = f"{_RACCOON_IMAGE_RAW}/{bundle}.json"
    try:
        resp = httpx.get(url, headers={"User-Agent": "raccoon-cli"}, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning("Bundle manifest %s returned HTTP %s", url, resp.status_code)
            return None
        data = resp.json()
        if "ref" in data and "components" not in data:
            return fetch_bundle_manifest(data["ref"])
        data.setdefault("bundle", bundle)
        return data
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch bundle manifest %s: %s", url, e)
    return None


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


def get_pi_versions_http(server_url: str, api_token: Optional[str] = None) -> Optional[dict[str, Optional[str]]]:
    """Fetch all Pi component versions from the raccoon-server /version endpoint.

    Returns a dict mapping package name to version string (or None if not installed),
    or None if the server is unreachable.
    """
    headers: dict[str, str] = {}
    if api_token:
        headers["X-API-Token"] = api_token
    try:
        resp = httpx.get(
            f"{server_url.rstrip('/')}/version",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


def _get_remote_version_ssh(ssh_client, pkg: PackageInfo) -> Optional[str]:
    """Get installed version of a non-pip Pi package via SSH.

    Runs pkg.version_cmd and returns its stdout, or None on failure.
    """
    if not pkg.version_cmd:
        return None
    try:
        _, stdout, _ = ssh_client.exec_command(pkg.version_cmd, timeout=10)
        output = stdout.read().decode().strip()
        return output if output else None
    except Exception:
        return None


def check_all_versions(
    ssh_client=None,
    server_url: Optional[str] = None,
    api_token: Optional[str] = None,
    manifest: Optional[dict] = None,
) -> list[PackageStatus]:
    """Aggregate version info for all packages in the registry.

    Fetches latest versions from GitHub and installed versions from local pip
    and (if connected) the Pi. Pi versions are fetched via the server HTTP
    endpoint when available, falling back to SSH.
    """
    latest_cache: dict[str, Optional[str]] = {}

    # Try HTTP first for Pi versions — single call, no trust-me files
    pi_versions_http: Optional[dict[str, Optional[str]]] = None
    if server_url:
        pi_versions_http = get_pi_versions_http(server_url, api_token)

    statuses: list[PackageStatus] = []
    for pkg in PACKAGE_REGISTRY:
        status = PackageStatus(info=pkg)

        # Latest version — prefer bundle manifest, fall back to individual GitHub release
        manifest_versions = (manifest or {}).get("components", {})
        if pkg.name in manifest_versions:
            tag = manifest_versions[pkg.name]
            status.latest_version = tag.lstrip("v") if tag else None
        else:
            if pkg.repo not in latest_cache:
                latest_cache[pkg.repo] = get_latest_version(pkg.repo)
            status.latest_version = latest_cache[pkg.repo]

        # Laptop version (local pip)
        if "laptop" in pkg.targets and pkg.pip_name:
            status.laptop_version = get_local_pip_version(pkg.pip_name)

        # Pi version — HTTP preferred, SSH fallback
        if "pi" in pkg.targets:
            if pi_versions_http is not None:
                status.pi_version = pi_versions_http.get(pkg.name)
            elif ssh_client:
                if pkg.pip_name:
                    status.pi_version = get_remote_pip_version(ssh_client, pkg.pip_name)
                else:
                    status.pi_version = _get_remote_version_ssh(ssh_client, pkg)

        statuses.append(status)

    return statuses


_NON_VERSION_TAGS = {"dev", "installed"}


def _parse_version(v: str):
    try:
        from packaging.version import Version
        return Version(v)
    except Exception:
        return tuple(int(x) for x in v.split(".") if x.isdigit())


def version_is_newer(installed: str, target: str) -> bool:
    """Return True if installed is strictly newer than target."""
    try:
        return _parse_version(installed) > _parse_version(target)
    except Exception:
        return False


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
    if version_is_newer(installed, latest):
        return installed, "blue"   # ahead of bundle — shown in blue
    return installed, "yellow"     # behind bundle


def render_version_table(console: Console, statuses: list[PackageStatus]) -> tuple[bool, bool]:
    """Render a Rich table showing package version status.

    Returns (any_outdated, any_ahead).
    """
    table = Table(title="Package Versions")
    table.add_column("Package", style="cyan")
    table.add_column("Bundle", style="dim")
    table.add_column("Laptop", justify="center")
    table.add_column("Pi", justify="center")

    any_outdated = False
    any_ahead = False

    for s in statuses:
        latest_display = s.latest_version or "?"

        def _cell(version: Optional[str]) -> str:
            text, style = _version_style(version, s.latest_version)
            return f"[{style}]{text}[/{style}]"

        def _check(version: Optional[str]) -> None:
            nonlocal any_outdated, any_ahead
            if not version or version in _NON_VERSION_TAGS or not s.latest_version:
                return
            if version_is_newer(version, s.latest_version):
                any_ahead = True
            elif version != s.latest_version:
                any_outdated = True

        laptop_cell = _cell(s.laptop_version) if "laptop" in s.info.targets else "[dim]n/a[/dim]"
        pi_cell = _cell(s.pi_version) if "pi" in s.info.targets else "[dim]n/a[/dim]"

        if "laptop" in s.info.targets:
            _check(s.laptop_version)
        if "pi" in s.info.targets:
            _check(s.pi_version)

        table.add_row(s.info.name, latest_display, laptop_cell, pi_cell)

    console.print(table)
    return any_outdated, any_ahead
