"""The raccoon-server post-install hands over to raccoon-cam's own post-install, when it is installed."""

import subprocess
import sys

from click.testing import CliRunner

from raccoon_cli.server import cli


def _run_post_install(monkeypatch, *, cam_installed: bool, cam_returncode: int = 0):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, cam_returncode if "raccoon_cam.post_install" in command else 0)

    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_do_install", lambda user: None)
    monkeypatch.setattr(cli, "_ensure_hotspot_packages", lambda: None)
    monkeypatch.setattr(cli, "_install_shared_dnsmasq_config", lambda: None)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli.importlib.util, "find_spec", lambda name: object() if (cam_installed and name == "raccoon_cam") else None
    )
    result = CliRunner().invoke(cli.main, ["post-install"])
    return result, calls


def test_runs_the_raccoon_cam_post_install_before_restarting_the_service(monkeypatch):
    result, calls = _run_post_install(monkeypatch, cam_installed=True)
    assert result.exit_code == 0
    cam = [sys.executable, "-m", "raccoon_cam.post_install"]
    restart = ["systemctl", "restart", "raccoon.service"]
    assert cam in calls and restart in calls
    assert calls.index(cam) < calls.index(restart)


def test_does_nothing_when_raccoon_cam_is_not_installed(monkeypatch):
    result, calls = _run_post_install(monkeypatch, cam_installed=False)
    assert result.exit_code == 0
    assert not any("raccoon_cam.post_install" in command for command in calls)


def test_a_failing_raccoon_cam_post_install_only_warns(monkeypatch):
    result, calls = _run_post_install(monkeypatch, cam_installed=True, cam_returncode=1)
    assert result.exit_code == 0
    assert "raccoon-cam post-install reported problems" in result.output
    assert ["systemctl", "restart", "raccoon.service"] in calls


def test_raccoon_cam_is_a_pi_wheel_in_the_package_registry():
    from raccoon_cli.version_checker import PACKAGE_REGISTRY

    cam = next(p for p in PACKAGE_REGISTRY if p.name == "raccoon-cam")
    assert (cam.pip_name, cam.pi_install, cam.targets) == ("raccoon-cam", "pypi", ["pi"])
