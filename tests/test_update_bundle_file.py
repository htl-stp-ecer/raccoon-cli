"""raccoon update: bundle file and restart come after everything else is installed."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from rich.console import Console

from raccoon_cli.commands import update as upd

MANIFEST = {
    "bundle": "2026.11.02",
    "contract_hash": "93ac36836810c9fe",
    "components": {"raccoon-transport": "v69", "raccoon-cli": "v173"},
}


class FakeSsh:
    def __init__(self, exit_code=0):
        self.commands = []
        self.uploads = []
        self.exit_code = exit_code

    def open_sftp(self):
        sftp = MagicMock()
        sftp.putfo.side_effect = lambda fileobj, path: self.uploads.append((path, fileobj.read()))
        return sftp

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = self.exit_code
        stderr = MagicMock()
        stderr.read.return_value = b"denied" if self.exit_code else b""
        return None, stdout, stderr


def test_bundle_file_is_uploaded_and_installed_with_root_ownership():
    ssh = FakeSsh()
    assert upd._install_bundle_file(Console(), ssh, MANIFEST) is True
    path, data = ssh.uploads[0]
    assert path == "/tmp/raccoon-bundle.json"
    assert json.loads(data) == MANIFEST
    assert "install -m 644 -o root -g root /tmp/raccoon-bundle.json /etc/raccoon-bundle.json" in ssh.commands[0]
    assert "/run/raccoon" in ssh.commands[0]


def test_failure_is_reported_and_means_no_restart():
    assert upd._install_bundle_file(Console(), FakeSsh(exit_code=1), MANIFEST) is False


def test_restart_covers_the_services_and_server_last():
    ssh = FakeSsh()
    upd._restart_pi_services(Console(), ssh)
    assert ssh.commands[0] == "sudo systemctl restart stm32_data_reader.service flutter-ui.service"
    assert ssh.commands[-1] == "sudo systemctl --no-block restart raccoon.service"


def run_update(monkeypatch, resolved_args, manifest=MANIFEST):
    """Run `raccoon update` with one Pi package to install; return the ordered list of steps."""
    from click.testing import CliRunner
    from rich.console import Console as C

    from raccoon_cli.version_checker import PACKAGE_REGISTRY, PackageStatus

    steps = []
    ssh = FakeSsh()
    info = next(p for p in PACKAGE_REGISTRY if p.name == "raccoon-lib")
    statuses = [PackageStatus(info=info, latest_version="155", pi_version="154")]
    monkeypatch.setattr(upd, "find_project_root", lambda: "/project")
    monkeypatch.setattr(upd, "_get_ssh_client", lambda console: ssh)
    monkeypatch.setattr(upd, "get_connection_manager", lambda: MagicMock(state=MagicMock(pi_address="1.2.3.4", pi_port=8421, api_token=None)))
    monkeypatch.setattr(upd, "fetch_bundle_manifest", lambda tag: manifest)
    monkeypatch.setattr(upd, "check_all_versions", lambda **kw: statuses)
    monkeypatch.setattr(upd, "render_version_table", lambda *a, **kw: (True, False))
    monkeypatch.setattr(upd, "_update_pi", lambda *a, **kw: steps.append("install"))
    monkeypatch.setattr(upd, "_run_post_install", lambda *a, **kw: steps.append("post-install"))
    monkeypatch.setattr(upd, "_install_bundle_file", lambda *a, **kw: steps.append("bundle-file") or True)
    monkeypatch.setattr(upd, "_mark_components_dev", lambda *a, **kw: steps.append("mark-dev") or True)
    monkeypatch.setattr(upd, "_restart_pi_services", lambda *a, **kw: steps.append("restart"))
    result = CliRunner().invoke(upd.update_command, ["--pi-only", *resolved_args], obj={"console": C()}, input="y\n")
    assert result.exit_code == 0, result.output
    return steps


def test_order_is_install_then_bundle_file_then_restart(monkeypatch):
    assert run_update(monkeypatch, []) == ["install", "post-install", "bundle-file", "restart"]


def test_dev_update_marks_the_installed_components_as_dev(monkeypatch):
    dev = {"bundle": "dev", "components": {"raccoon-lib": "v155", "raccoon-transport": ""}}
    assert run_update(monkeypatch, ["--dev"], manifest=dev) == ["install", "post-install", "mark-dev", "restart"]


class FileSsh(FakeSsh):
    """FakeSsh whose sftp can also read an existing /etc/raccoon-bundle.json."""

    def __init__(self, existing=None):
        super().__init__()
        self.existing = existing

    def open_sftp(self):
        sftp = super().open_sftp()
        if self.existing is None:
            sftp.open.side_effect = FileNotFoundError
        else:
            handle = MagicMock()
            handle.__enter__.return_value.read.return_value = json.dumps(self.existing).encode()
            sftp.open.return_value = handle
        return sftp


def test_mark_dev_changes_only_the_named_components():
    existing = {
        "bundle": "2026.11.03",
        "contract_hash": "93ac36836810c9fe",
        "components": {"raccoon-lib": "v156", "raccoon-cli": "v174", "botui": "v75"},
    }
    ssh = FileSsh(existing)
    assert upd._mark_components_dev(Console(), ssh, ["raccoon-lib", "botui"], None) is True
    written = json.loads(ssh.uploads[0][1])
    assert written["components"] == {"raccoon-lib": "dev", "raccoon-cli": "v174", "botui": "dev"}
    assert written["bundle"] == "2026.11.03"
    assert written["contract_hash"] == "93ac36836810c9fe"


def test_mark_dev_without_a_file_starts_from_the_dev_manifest():
    dev = {"bundle": "dev", "components": {"raccoon-lib": "v155", "botui": "v77"}}
    ssh = FileSsh(None)
    assert upd._mark_components_dev(Console(), ssh, ["botui"], dev) is True
    written = json.loads(ssh.uploads[0][1])
    assert written["bundle"] == "dev"
    assert written["components"] == {"raccoon-lib": "v155", "botui": "dev"}


def test_mark_dev_with_nothing_installed_writes_nothing():
    ssh = FileSsh({"bundle": "x", "components": {}})
    assert upd._mark_components_dev(Console(), ssh, [], None) is False
    assert ssh.uploads == []


def test_no_pi_connection_acts_like_laptop_only(monkeypatch):
    from click.testing import CliRunner
    from rich.console import Console as C

    seen = {}
    monkeypatch.setattr(upd, "find_project_root", lambda: "/project")
    monkeypatch.setattr(upd, "_get_ssh_client", lambda console: None)
    monkeypatch.setattr(upd, "get_connection_manager", lambda: MagicMock(state=MagicMock(pi_address="1.2.3.4", pi_port=8421, api_token="t")))
    monkeypatch.setattr(upd, "fetch_bundle_manifest", lambda tag: MANIFEST)

    def check(**kw):
        seen["check"] = kw
        return []

    def table(console, statuses, show_laptop, show_pi):
        seen["table"] = (show_laptop, show_pi)
        return (False, False)

    monkeypatch.setattr(upd, "check_all_versions", check)
    monkeypatch.setattr(upd, "render_version_table", table)
    result = CliRunner().invoke(upd.update_command, [], obj={"console": C()})
    assert result.exit_code == 0, result.output
    assert seen["table"] == (True, False)
    assert seen["check"]["server_url"] is None and seen["check"]["api_token"] is None
