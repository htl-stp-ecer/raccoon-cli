"""Version endpoint — reports installed versions of all RaccoonOS components."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

router = APIRouter(tags=["version"])

_BOTUI_VERSION_FILE = "/home/pi/stp-velox/version"
# The .deb installs the reader to /usr/bin; the old tarball install used /home/pi/stm32_data_reader.
_STM32_BINARIES = ("/usr/bin/stm32_data_reader", "/home/pi/stm32_data_reader/stm32_data_reader")


def _pip_version_subprocess(package: str) -> Optional[str]:
    for cmd in [["pip3", "show", package], ["pip", "show", package]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            continue
    return None


def _pip_version(package: str) -> Optional[str]:
    """Installed version of a Python package.

    Read in this process from the package's dist-info (milliseconds) instead of
    starting ``pip3 show`` (a Python start plus pip's imports per package, slow on a
    Pi 3). Only when this interpreter does not see the package at all, e.g. it was
    installed into another environment, ask pip as before.
    """
    try:
        return metadata.version(package) or None
    except metadata.PackageNotFoundError:
        return _pip_version_subprocess(package)
    except Exception:
        return _pip_version_subprocess(package)


def _binary_version(binary_path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


def _stm32_version() -> Optional[str]:
    for binary in _STM32_BINARIES:
        version = _binary_version(binary)
        if version:
            return version
    return None


def _file_version(path: str) -> Optional[str]:
    try:
        content = Path(path).read_text().strip()
        return content or None
    except Exception:
        return None


@router.get("/version")
async def get_versions() -> dict:
    """Return installed versions of all RaccoonOS components on this Pi.

    Each value is the version string if installed, or null if not found.
    This endpoint queries the actual installed artifacts — no cached state.
    The lookups run in worker threads at the same time: they block (files, a
    ``--version`` subprocess) and must neither queue behind each other nor stall
    the event loop of the server.
    """
    # Package metadata is looked up on disk each time; drop the import system's own
    # path caches too so a package installed a moment ago is seen right away.
    importlib.invalidate_caches()
    cli, lib, transport, cam, stm32, ui_version = await asyncio.gather(
        asyncio.to_thread(_pip_version, "raccoon-cli"),
        asyncio.to_thread(_pip_version, "raccoon-library"),
        asyncio.to_thread(_pip_version, "raccoon-transport"),
        asyncio.to_thread(_pip_version, "raccoon-cam"),
        asyncio.to_thread(_stm32_version),
        asyncio.to_thread(_file_version, _BOTUI_VERSION_FILE),
    )
    return {
        "raccoon-cli": cli,
        "raccoon-lib": lib,
        "raccoon-transport": transport,
        "stm32-data-reader": stm32,
        "raccoon-cam": cam,
        "botui": ui_version,
        "ui": ui_version,
    }
