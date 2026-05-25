from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raccoon_cli.server.routes import version as version_route
from raccoon_cli.version_checker import resolve_pypi_version


def test_resolve_pypi_version_fails_when_requested_bundle_version_is_missing(monkeypatch):
    monkeypatch.setattr(
        "raccoon_cli.version_checker.get_pypi_versions",
        lambda pip_name: ["1.2.0", "1.3.0"],
    )

    with pytest.raises(ValueError, match="allow-missing-pypi-version-fallback"):
        resolve_pypi_version("raccoon-cli", "9.9.9")


def test_resolve_pypi_version_allows_explicit_fallback(monkeypatch):
    monkeypatch.setattr(
        "raccoon_cli.version_checker.get_pypi_versions",
        lambda pip_name: ["1.2.0", "1.3.0"],
    )

    resolved, note = resolve_pypi_version(
        "raccoon-cli",
        "9.9.9",
        allow_missing_fallback=True,
    )

    assert resolved == "1.3.0"
    assert note == "raccoon-cli 9.9.9 is not on PyPI; using 1.3.0 instead."


def test_version_route_checks_raccoon_library_only(monkeypatch):
    calls: list[str] = []

    def fake_pip_version(package: str):
        calls.append(package)
        versions = {
            "raccoon-cli": "0.1.154",
            "raccoon-library": "1.0.101",
            "raccoon-transport": "0.1.200",
        }
        return versions.get(package)

    monkeypatch.setattr(version_route, "_pip_version", fake_pip_version)
    monkeypatch.setattr(version_route, "_binary_version", lambda path: None)
    monkeypatch.setattr(version_route, "_file_version", lambda path: "2026.4.25.1")

    data = asyncio.run(version_route.get_versions())

    assert "raccoon" not in calls
    assert "raccoon-library" in calls
    assert data["raccoon-lib"] == "1.0.101"
    assert data["botui"] == "2026.4.25.1"
    assert data["ui"] == "2026.4.25.1"
