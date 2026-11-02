"""Shared version checking logic for raccoon update and status commands."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import dataclass
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
    # How the package is installed on the Pi:
    #   "pypi"       - ``pip3 install pip_name[extras]==version`` over SSH
    #   "tarball"    - GitHub release tarball whose ``install.py`` runs on the laptop
    #   "deb"        - GitHub release .deb, uploaded and installed with apt
    #   "dependency" - pulled in by other packages; nothing to install itself
    pi_install: str = "tarball"
    # fnmatch pattern for the release asset (tarball / deb installs).
    pi_asset: Optional[str] = None
    # pip extras installed together with the package on the Pi.
    pi_extras: Optional[str] = None


PACKAGE_REGISTRY: list[PackageInfo] = [
    PackageInfo(
        name="raccoon-cli",
        repo="htl-stp-ecer/raccoon-cli",
        pip_name="raccoon-cli",
        targets=["laptop", "pi"],
        pi_install="tarball",
        pi_asset="raccoon-server-v*.tar.gz",
    ),
    PackageInfo(
        name="raccoon-lib",
        repo="htl-stp-ecer/raccoon-lib",
        pip_name="raccoon-library",
        targets=["laptop", "pi"],
        pi_install="pypi",
        pi_extras="sim",
    ),
    PackageInfo(
        name="raccoon-transport",
        repo="htl-stp-ecer/raccoon-transport",
        pip_name="raccoon-transport",
        targets=["pi"],
        # Installed exactly as in the bundle (together with raccoon-library, in one pip run),
        # so the Pi never ends up on a newer transport than the bundle was checked against.
        pi_install="pypi",
    ),
    PackageInfo(
        name="botui",
        repo="htl-stp-ecer/botui",
        pip_name=None,
        targets=["pi"],
        version_cmd="cat /home/pi/stp-velox/version 2>/dev/null",
        pi_install="deb",
        pi_asset="stp-velox_*_arm64.deb",
    ),
    PackageInfo(
        name="stm32-data-reader",
        repo="htl-stp-ecer/stm32-data-reader",
        pip_name=None,
        targets=["pi"],
        version_cmd="/home/pi/stm32_data_reader/stm32_data_reader --version 2>/dev/null",
        pi_install="tarball",
        pi_asset="stm32_data_reader-v*.tar.gz",
    ),
    PackageInfo(
        name="raccoon-cam",
        repo="htl-stp-ecer/raccoon-cam",
        pip_name=None,
        targets=["pi"],
        version_cmd="/usr/local/bin/raccoon-cam --version 2>/dev/null",
        pi_install="tarball",
        pi_asset="*.tar.gz",
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


def download_release_assets_for_version(
    repo: str, pattern: str, dest_dir: str, version: str
) -> list[str]:
    """Download assets from the release of a specific version.

    Release tags are usually ``v<version>`` but some repos tag without the
    ``v``, so both spellings are tried.
    """
    for tag in (f"v{version}", version):
        files = download_release_assets(repo, pattern, dest_dir, tag=tag)
        if files:
            return files
    return []


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


def get_local_pip_version(pip_name: str) -> Optional[str]:
    """Get the installed version of a package in the running environment.

    Returns "dev" for editable/local installs.
    """
    from importlib import metadata

    try:
        dist = metadata.distribution(pip_name)
    except metadata.PackageNotFoundError:
        return None
    try:
        direct_url = dist.read_text("direct_url.json")
        if direct_url and json.loads(direct_url).get("url", "").startswith("file://"):
            return "dev"
    except Exception:
        pass
    return dist.version


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

        # Latest version — the bundle manifest is authoritative. A component it
        # doesn't list (or lists empty) is not part of the bundle: leave the
        # version unset so it shows as "-" and is never touched. Only when no
        # manifest could be fetched at all, fall back to each repo's GitHub release.
        if manifest is not None:
            tag = (manifest.get("components") or {}).get(pkg.name)
            status.latest_version = (
                tag.strip().lstrip("v") or None if isinstance(tag, str) else None
            )
        else:
            if pkg.repo not in latest_cache:
                latest_cache[pkg.repo] = get_latest_version(pkg.repo)
            status.latest_version = latest_cache[pkg.repo]

        # Laptop version (local pip)
        if "laptop" in pkg.targets and pkg.pip_name:
            status.laptop_version = get_local_pip_version(pkg.pip_name)

        # Pi version — HTTP only; if /version endpoint missing, leave as None → update everything
        if "pi" in pkg.targets and pi_versions_http is not None:
            status.pi_version = _resolve_pi_version(pkg.name, pi_versions_http)

        statuses.append(status)

    return statuses


_NON_VERSION_TAGS = {"dev", "installed"}


def _parse_version(v: str):
    try:
        from packaging.version import Version
        return Version(v)
    except Exception:
        return tuple(int(x) for x in v.split(".") if x.isdigit())


def _resolve_pi_version(name: str, versions: dict[str, Optional[str]]) -> Optional[str]:
    if name == "botui":
        return versions.get("botui") or versions.get("ui")
    return versions.get(name)


_PYPI_API = "https://pypi.org/pypi"


def get_pypi_versions(pip_name: str) -> Optional[list[str]]:
    """Fetch available versions for a PyPI package."""
    url = f"{_PYPI_API}/{pip_name}/json"
    try:
        resp = httpx.get(url, headers={"User-Agent": "raccoon-cli"}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return list((data.get("releases") or {}).keys())
    except httpx.HTTPError:
        pass
    return None


def resolve_pypi_version(
    pip_name: str,
    requested: Optional[str],
    allow_missing_fallback: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a PyPI version.

    By default, a missing requested version is treated as an error so bundle
    updates stay exact. Callers may opt into falling back to the latest PyPI
    release with ``allow_missing_fallback=True``.
    """
    versions = get_pypi_versions(pip_name)
    if not versions:
        return requested, None
    if requested and requested in versions:
        return requested, None
    latest = max(versions, key=_parse_version)
    if requested:
        if not allow_missing_fallback:
            raise ValueError(
                f"{pip_name} {requested} is not on PyPI. "
                "Re-run with --allow-missing-pypi-version-fallback to install "
                f"{latest} instead."
            )
        return latest, f"{pip_name} {requested} is not on PyPI; using {latest} instead."
    return latest, None


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


def _render_target_table(
    console: Console,
    statuses: list[PackageStatus],
    target: str,
    title: str,
) -> tuple[bool, bool]:
    """Render one table (laptop or Pi). Returns (any_outdated, any_ahead)."""
    table = Table(title=title)
    table.add_column("Package", style="cyan")
    table.add_column("Bundle", style="dim")
    table.add_column("Installed", justify="center")

    any_outdated = False
    any_ahead = False

    for s in statuses:
        if target not in s.info.targets:
            continue
        installed = s.laptop_version if target == "laptop" else s.pi_version
        text, style = _version_style(installed, s.latest_version)
        table.add_row(s.info.name, s.latest_version or "-", f"[{style}]{text}[/{style}]")

        if installed and installed not in _NON_VERSION_TAGS and s.latest_version:
            if version_is_newer(installed, s.latest_version):
                any_ahead = True
            elif installed != s.latest_version:
                any_outdated = True

    console.print(table)
    return any_outdated, any_ahead


def render_version_table(
    console: Console,
    statuses: list[PackageStatus],
    show_laptop: bool = True,
    show_pi: bool = True,
) -> tuple[bool, bool]:
    """Render the laptop table, then the Pi table, each preceded by a blank line.

    Returns (any_outdated, any_ahead).
    """
    any_outdated = False
    any_ahead = False
    for target, title, enabled in (
        ("laptop", "Laptop Versions", show_laptop),
        ("pi", "Pi Versions", show_pi),
    ):
        if not enabled:
            continue
        console.print()
        outdated, ahead = _render_target_table(console, statuses, target, title)
        any_outdated |= outdated
        any_ahead |= ahead
    return any_outdated, any_ahead
