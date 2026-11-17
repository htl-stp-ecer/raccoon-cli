import asyncio
import time
from importlib import metadata

from raccoon_cli.server.routes import version


def test_pip_version_reads_metadata_without_starting_pip(monkeypatch):
    monkeypatch.setattr(metadata, "version", lambda name: "1.2.3")

    def no_subprocess(*args, **kwargs):
        raise AssertionError("pip must not be started when the package is importable metadata")

    monkeypatch.setattr(version.subprocess, "run", no_subprocess)

    assert version._pip_version("raccoon-cam") == "1.2.3"


def test_pip_version_falls_back_to_pip_when_metadata_does_not_see_it(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    monkeypatch.setattr(version, "_pip_version_subprocess", lambda name: "9.9" if name == "x" else None)

    assert version._pip_version("x") == "9.9"
    assert version._pip_version("y") is None


def test_version_route_runs_the_lookups_at_the_same_time(monkeypatch):
    delay = 0.2

    def slow(*args):
        time.sleep(delay)
        return "1"

    monkeypatch.setattr(version, "_pip_version", slow)
    monkeypatch.setattr(version, "_stm32_version", slow)
    monkeypatch.setattr(version, "_file_version", slow)

    started = time.monotonic()
    data = asyncio.run(version.get_versions())
    elapsed = time.monotonic() - started

    assert data["raccoon-cam"] == "1" and data["stm32-data-reader"] == "1" and data["botui"] == "1"
    # Six lookups of 0.2 s one after another would take 1.2 s.
    assert elapsed < delay * 3
