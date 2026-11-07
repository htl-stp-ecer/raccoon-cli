from subprocess import CompletedProcess

from raccoon_cli.server import cli


def _patch(monkeypatch, installed, apt_codes):
    calls: list = []
    codes = iter(apt_codes)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "apt-get" and cmd[1] == "install":
            return CompletedProcess(cmd, next(codes), stdout="", stderr="E: no network")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/sbin/x" if installed else None)
    return calls


def test_skips_when_already_installed(monkeypatch):
    calls = _patch(monkeypatch, True, [])
    cli._ensure_hotspot_packages()
    assert calls == []


def test_installs_and_disables_services(monkeypatch):
    calls = _patch(monkeypatch, False, [0])
    cli._ensure_hotspot_packages()
    assert calls[0][:2] == ["apt-get", "install"]
    assert ["systemctl", "disable", "--now", "dnsmasq"] in calls
    assert ["systemctl", "disable", "--now", "hostapd"] in calls
    assert ["systemctl", "mask", "hostapd"] in calls


def test_retries_after_apt_update(monkeypatch):
    calls = _patch(monkeypatch, False, [100, 0])
    cli._ensure_hotspot_packages()
    assert ["apt-get", "update"] in calls
    assert sum(1 for c in calls if c[:2] == ["apt-get", "install"]) == 2


def test_offline_failure_is_not_fatal_and_leaves_services_alone(monkeypatch):
    calls = _patch(monkeypatch, False, [100, 100])
    cli._ensure_hotspot_packages()
    assert not any(c[0] == "systemctl" for c in calls)


def test_shared_dnsmasq_config_removes_router_and_dns(tmp_path, monkeypatch):
    target = tmp_path / "dnsmasq-shared.d" / "raccoon.conf"
    monkeypatch.setattr(cli, "SHARED_DNSMASQ_CONF", target)

    cli._install_shared_dnsmasq_config()

    lines = target.read_text().splitlines()
    assert "dhcp-option=3" in lines
    assert "dhcp-option=6" in lines
