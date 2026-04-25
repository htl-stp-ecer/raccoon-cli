"""Version endpoint — reports installed versions of all RaccoonOS components."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

router = APIRouter(tags=["version"])

_BOTUI_VERSION_FILE = "/home/pi/stp-velox/version"
_STM32_BINARY = "/home/pi/stm32_data_reader/stm32_data_reader"
_CAM_BINARY = "/usr/local/bin/raccoon-cam"


def _pip_version(package: str) -> Optional[str]:
    for cmd in [["pip3", "show", package], ["pip", "show", package]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            continue
    return None


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
    """
    return {
        "raccoon-cli": _pip_version("raccoon-cli"),
        "raccoon-lib": _pip_version("raccoon"),
        "raccoon-transport": _pip_version("raccoon-transport"),
        "stm32-data-reader": _binary_version(_STM32_BINARY),
        "raccoon-cam": _binary_version(_CAM_BINARY),
        "botui": _file_version(_BOTUI_VERSION_FILE),
    }
