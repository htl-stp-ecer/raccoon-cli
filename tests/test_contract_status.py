"""Transport contract status: server endpoint, run refusal, CLI display."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from rich.console import Console

from raccoon_cli import contract_status

pytestmark = pytest.mark.skipif(
    not contract_status.available(), reason="installed raccoon-transport has no contract check"
)

HASH = "93ac36836810c9fe"


@pytest.fixture
def device(tmp_path, monkeypatch):
    """A fake Raccoon device: marker dir, bundle file and status dir under tmp_path."""
    (tmp_path / "image").mkdir()
    monkeypatch.setenv("RACCOON_DEVICE_MARKER", str(tmp_path / "image"))
    monkeypatch.setenv("RACCOON_BUNDLE_FILE", str(tmp_path / "bundle.json"))
    monkeypatch.setenv("RACCOON_STATUS_DIR", str(tmp_path / "run"))

    class Device:
        bundle = tmp_path / "bundle.json"

        def write_bundle(self, contract_hash=HASH, **versions):
            components = {"raccoon-transport": "v69", "stm32-data-reader": "v1", "a": "v1", "b": "v1",
                          "raccoon-cli": "173", **versions}
            self.bundle.write_text(
                json.dumps({"bundle": "2026.11.02", "contract_hash": contract_hash, "components": components})
            )

        def report(self, component, own_hash, persistent=True):
            from raccoon_transport import contract_check as cc

            result = cc.check(component, "v1", own_hash=own_hash, own_version="v69")
            cc.write_status(result, persistent=persistent)

    return Device()


def test_all_matching_is_ok(device):
    device.write_bundle()
    device.report("stm32-data-reader", HASH)
    summary = contract_status.local_summary()
    assert summary["ok"] and summary["available"]
    assert summary["expected_hash"] == HASH
    assert contract_status.refusal_message(summary) is None


def test_mismatch_refuses_and_names_the_component(device):
    device.write_bundle()
    device.report("stm32-data-reader", "1111111111111111")
    message = contract_status.refusal_message(contract_status.local_summary())
    assert message and "stm32-data-reader" in message and "refusing to start" in message


def test_missing_bundle_file_refuses(device):
    message = contract_status.refusal_message(contract_status.local_summary())
    assert message and "file-missing" in message


def test_dev_mode_never_refuses_and_is_announced(device):
    device.write_bundle("dev")
    device.report("a", "1111111111111111")
    device.report("b", "2222222222222222")
    summary = contract_status.local_summary()
    assert contract_status.refusal_message(summary) is None
    assert "DEV mode" in contract_status.dev_mode_line(summary)
    assert "different transports" in contract_status.dev_mode_line(summary)


def test_not_a_device_is_not_blocked(tmp_path, monkeypatch):
    """A laptop (no device marker) must be able to run the server and runs."""
    monkeypatch.setenv("RACCOON_DEVICE_MARKER", str(tmp_path / "nope"))
    monkeypatch.setenv("RACCOON_BUNDLE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("RACCOON_STATUS_DIR", str(tmp_path / "run"))
    summary = contract_status.local_summary()
    assert summary["ok"] and summary["on_device"] is False
    assert contract_status.refusal_message(summary) is None
    console = Console(record=True, width=100)
    assert contract_status.render_summary(console, summary) is True
    assert "not running on a Raccoon device" in console.export_text()


def test_server_records_its_own_check_under_the_cli_entry(device):
    device.write_bundle()
    contract_status.record_server_start("173")
    names = [c["component"] for c in contract_status.local_summary()["components"]]
    assert "raccoon-server" in names
    assert contract_status.local_summary()["ok"]


def test_server_version_is_compared_with_the_raccoon_cli_entry(device):
    device.write_bundle(**{"raccoon-cli": "v174"})
    contract_status.record_server_start("173")
    entry = [c for c in contract_status.local_summary()["components"] if c["component"] == "raccoon-server"][0]
    assert entry["result"] == "version-mismatch"
    message = contract_status.refusal_message(contract_status.local_summary())
    assert message and "raccoon-server" in message


def test_component_with_dev_entry_is_not_compared(device):
    device.write_bundle(**{"stm32-data-reader": "dev"})
    device.report("stm32-data-reader", HASH)
    summary = contract_status.local_summary()
    assert summary["ok"]
    entry = [c for c in summary["components"] if c["component"] == "stm32-data-reader"][0]
    assert entry["result"] == "dev-warnung" and entry["reason"] == "version-omitted"


def test_endpoint_returns_the_summary(device):
    from raccoon_cli import __version__
    from raccoon_cli.server.app import create_app

    # create_app() starts the server's own check, which compares the real version.
    device.write_bundle(**{"raccoon-cli": __version__})
    device.report("stm32-data-reader", HASH)
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/contract").json()
    assert body["ok"] is True
    assert {c["component"] for c in body["components"]} >= {"stm32-data-reader"}


@pytest.mark.asyncio
async def test_run_is_refused_with_409_and_nothing_is_started(device, monkeypatch):
    from raccoon_cli.server.routes import commands

    device.write_bundle()
    device.report("stm32-data-reader", "1111111111111111")
    commands._active_commands.clear()
    with pytest.raises(HTTPException) as raised:
        await commands._start_command("proj", commands.CommandType.RUN, ["run"], {})
    assert raised.value.status_code == 409
    assert "stm32-data-reader" in raised.value.detail
    assert commands._active_commands == {}


def test_render_summary_shows_table_and_failure(device):
    device.write_bundle()
    device.report("stm32-data-reader", "1111111111111111")
    console = Console(record=True, width=120)
    ok = contract_status.render_summary(console, contract_status.local_summary())
    text = console.export_text()
    assert ok is False
    assert "stm32-data-reader" in text and "mismatch" in text and "Runs are refused" in text


def test_render_summary_for_old_transport():
    console = Console(record=True, width=100)
    assert contract_status.render_summary(console, contract_status.unavailable_summary()) is True
    assert "no contract check" in console.export_text()


def test_server_error_text_prefers_the_server_detail():
    from raccoon_cli.commands.run import _server_error_text

    class Response:
        def json(self):
            return {"detail": "Transport contract check failed; refusing to start."}

    class Error(Exception):
        response = Response()

    assert _server_error_text(Error("409 Conflict")) == "Transport contract check failed; refusing to start."
    assert _server_error_text(ValueError("plain")) == "plain"
