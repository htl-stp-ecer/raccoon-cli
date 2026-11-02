"""raccoon update: the bundle must name a transport, and the Pi gets it pinned exactly."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from rich.console import Console

from raccoon_cli.commands import update as upd
from raccoon_cli.version_checker import PACKAGE_REGISTRY, PackageStatus


def status(name: str, latest: str | None, pi: str | None = None) -> PackageStatus:
    info = next(p for p in PACKAGE_REGISTRY if p.name == name)
    return PackageStatus(info=info, latest_version=latest, pi_version=pi)


# -- the required bundle field ----------------------------------------------

def test_bundle_without_transport_is_a_problem():
    manifest = {"bundle": "2026.5.25.1", "components": {"raccoon-lib": "v1", "raccoon-cli": "v2"}}
    assert "no raccoon-transport version" in upd.missing_transport_problem(manifest, "2026.5.25.1")


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_transport_field_is_a_problem(value):
    manifest = {"bundle": "b", "components": {"raccoon-transport": value, "raccoon-cli": "v2"}}
    assert upd.missing_transport_problem(manifest, "b")


def test_bundle_with_transport_is_fine():
    manifest = {"bundle": "b", "contract_hash": "93ac36836810c9fe", "components": {"raccoon-transport": "v69"}}
    assert upd.missing_transport_problem(manifest, "b") is None


@pytest.mark.parametrize("value", [None, "", "  ", "dev", 5])
def test_transport_without_a_usable_hash_is_a_problem(value):
    manifest = {"bundle": "b", "components": {"raccoon-transport": "v69"}}
    if value is not None:
        manifest["contract_hash"] = value
    assert "contract_hash" in upd.missing_transport_problem(manifest, "b")


def test_dev_bundle_is_exempt():
    manifest = {"bundle": "dev", "components": {"raccoon-lib": "v155", "raccoon-transport": ""}}
    assert upd.missing_transport_problem(manifest, "dev") is None
    assert upd.missing_transport_problem({"bundle": "dev", "components": {}}, "latest") is None


def test_update_stops_before_touching_anything_without_transport(monkeypatch):
    """No SSH work after the manifest, no check_all_versions, no installs."""
    monkeypatch.setattr(upd, "find_project_root", lambda: "/project")
    monkeypatch.setattr(upd, "_get_ssh_client", lambda console: None)
    monkeypatch.setattr(upd, "get_connection_manager", lambda: MagicMock(state=MagicMock(pi_address=None)))
    monkeypatch.setattr(upd, "fetch_bundle_manifest", lambda tag: {"bundle": "old", "components": {"raccoon-cli": "v1"}})
    called = []
    monkeypatch.setattr(upd, "check_all_versions", lambda **kw: called.append("versions"))
    monkeypatch.setattr(upd, "_update_laptop", lambda *a, **kw: called.append("laptop"))
    monkeypatch.setattr(upd, "_update_pi", lambda *a, **kw: called.append("pi"))

    result = CliRunner().invoke(upd.update_command, ["--bundle", "old"], obj={"console": Console()})

    assert result.exit_code == 1
    assert called == []


def test_check_only_also_reports_it(monkeypatch):
    monkeypatch.setattr(upd, "find_project_root", lambda: "/project")
    monkeypatch.setattr(upd, "_get_ssh_client", lambda console: None)
    monkeypatch.setattr(upd, "get_connection_manager", lambda: MagicMock(state=MagicMock(pi_address=None)))
    monkeypatch.setattr(upd, "fetch_bundle_manifest", lambda tag: {"bundle": "old", "components": {}})
    monkeypatch.setattr(upd, "check_all_versions", lambda **kw: pytest.fail("must stop earlier"))
    result = CliRunner().invoke(upd.update_command, ["--check", "--bundle", "old"], obj={"console": Console()})
    assert result.exit_code == 1


def test_fallback_without_manifest_cannot_skip_the_transport(monkeypatch):
    monkeypatch.setattr(upd, "find_project_root", lambda: "/project")
    monkeypatch.setattr(upd, "_get_ssh_client", lambda console: None)
    monkeypatch.setattr(upd, "get_connection_manager", lambda: MagicMock(state=MagicMock(pi_address=None)))
    monkeypatch.setattr(upd, "fetch_bundle_manifest", lambda tag: None)
    monkeypatch.setattr(upd, "check_all_versions", lambda **kw: [status("raccoon-transport", None)])
    monkeypatch.setattr(upd, "render_version_table", lambda *a, **kw: (False, False))
    installs = []
    monkeypatch.setattr(upd, "_update_pi", lambda *a, **kw: installs.append("pi"))
    result = CliRunner().invoke(upd.update_command, ["--pi-only"], obj={"console": Console()}, input="y\n")
    assert result.exit_code == 1
    assert installs == []


def test_dev_fallback_does_not_need_transport(monkeypatch):
    monkeypatch.setattr(upd, "find_project_root", lambda: "/project")
    monkeypatch.setattr(upd, "_get_ssh_client", lambda console: None)
    monkeypatch.setattr(upd, "get_connection_manager", lambda: MagicMock(state=MagicMock(pi_address=None)))
    monkeypatch.setattr(upd, "fetch_bundle_manifest", lambda tag: None)
    monkeypatch.setattr(upd, "check_all_versions", lambda **kw: [status("raccoon-transport", None)])
    monkeypatch.setattr(upd, "render_version_table", lambda *a, **kw: (False, False))
    result = CliRunner().invoke(upd.update_command, ["--dev", "--check"], obj={"console": Console()})
    assert result.exit_code == 0


# -- the exact pin on the Pi -------------------------------------------------

def resolve_same(s):
    return s.latest_version, None


def test_pin_is_added_to_the_pip_run():
    specs = upd.with_transport_pin(["raccoon-library[sim]==155"], status("raccoon-transport", "69"), resolve_same)
    assert specs == ["raccoon-library[sim]==155", "raccoon-transport==69"]


def test_pin_is_not_duplicated():
    specs = upd.with_transport_pin(["raccoon-transport==69", "raccoon-library==155"], status("raccoon-transport", "69"), resolve_same)
    assert specs.count("raccoon-transport==69") == 1 and len(specs) == 2


def test_no_pin_for_the_dev_bundle():
    assert upd.with_transport_pin(["raccoon-library==155"], None, resolve_same) == ["raccoon-library==155"]
    assert upd.with_transport_pin(["raccoon-library==155"], status("raccoon-transport", None), resolve_same) == ["raccoon-library==155"]


def test_transport_is_installed_when_it_differs_even_if_the_library_is_current():
    statuses = [status("raccoon-lib", "155", pi="155"), status("raccoon-transport", "69", pi="68")]
    names = [s.info.name for s in upd._get_pi_updates(statuses, force=False)]
    assert names == ["raccoon-transport"]


def test_pi_pip_run_pins_transport_with_the_library(monkeypatch):
    runs = []
    monkeypatch.setattr(upd, "resolve_pypi_version", lambda name, version, allow_missing_fallback: (version, None))
    monkeypatch.setattr(upd, "_pi_pip_install", lambda console, ssh, args, force: (runs.append(args) or True, ""))
    upd._pi_install_from_pypi(
        Console(), object(), [status("raccoon-lib", "155")], False, False, transport=status("raccoon-transport", "69")
    )
    assert runs == [["raccoon-library[sim]==155", "raccoon-transport==69"]]


def test_pi_pip_run_for_dev_has_no_transport_pin(monkeypatch):
    runs = []
    monkeypatch.setattr(upd, "resolve_pypi_version", lambda name, version, allow_missing_fallback: (version, None))
    monkeypatch.setattr(upd, "_pi_pip_install", lambda console, ssh, args, force: (runs.append(args) or True, ""))
    upd._pi_install_from_pypi(Console(), object(), [status("raccoon-lib", "155")], False, False, transport=None)
    assert runs == [["raccoon-library[sim]==155"]]
