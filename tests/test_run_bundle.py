"""Tests for per-run artifact bundling: local copy, server zip, client extract."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from raccoon_cli.logs import discover_log_files, load_run_by_index
from raccoon_cli.commands import logs as logs_cmd
from raccoon_cli.server.routes import logs as server_logs


def _make_run_dir(project: Path, run_id: str = "20260704T120000Z") -> Path:
    run_dir = project / ".raccoon" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "libstp.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"t": "2026-07-04T12:00:00.000", "elapsed": 0.0, "level": "info",
                 "file": "/x/api.py", "line": 1, "func": "f", "msg": "start"},
                {"t": "2026-07-04T12:00:01.000", "elapsed": 1.0, "level": "warning",
                 "file": "/x/m.py", "line": 2, "func": "g", "msg": "warn"},
            ]
        )
        + "\n"
    )
    (run_dir / "localization.jsonl").write_text('{"t_ns":0}\n{"t_ns":1000000000}\n')
    (run_dir / "profile.json").write_text('{"traceEvents":[]}\n')
    (run_dir / "run.json").write_text(json.dumps({"run_id": run_id}) + "\n")
    return run_dir


def _fake_journals() -> list[dict]:
    """Deterministic journal sections so tests never shell out to journalctl."""
    return [
        {
            "label": "raccoon-server", "unit": "raccoon.service",
            "file": "journal.raccoon-server.jsonl", "available": True,
            "entry_count": 1, "window_start_us": 0, "window_end_us": 1,
            "error": None,
            "entries": [{
                "timestamp": "2026-07-04T12:00:00+00:00", "level": "INFO",
                "message": "server up", "pid": "10", "identifier": "raccoon",
            }],
        },
        {
            "label": "stm32-data-reader", "unit": "stm32_data_reader.service",
            "file": "journal.stm32-data-reader.jsonl", "available": False,
            "entry_count": 0, "window_start_us": 0, "window_end_us": 1,
            "error": "unit not found", "entries": [],
        },
    ]


def _fake_ctx(project: Path) -> MagicMock:
    # discover_log_files scans the .raccoon/runs dir directly for <run_id>/ dirs.
    runs_dir = project / ".raccoon" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ctx = MagicMock()
    ctx.obj = {
        "console": MagicMock(),
        "log_dir_override": str(runs_dir),
        "show_all": False,
        "force_local": True,
    }
    return ctx


# ── local download ─────────────────────────────────────────────────


def test_download_local_bundles_whole_run_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(logs_cmd, "collect_journals", lambda u, s, e: _fake_journals())
    project = tmp_path
    _make_run_dir(project)
    out = tmp_path / "dl"

    logs_cmd._download_local(
        _fake_ctx(project), MagicMock(), run_id=1,
        output_dir=str(out), pad_secs=2.0,
        cmd_trace_path=str(tmp_path / "no_such_trace.jsonl"),
    )

    names = {p.name for p in out.iterdir() if p.is_file()}
    assert {"libstp.jsonl", "localization.jsonl", "profile.json", "run.json",
            "cmd_trace.jsonl", "manifest.json",
            "journal.raccoon-server.jsonl",
            "journal.stm32-data-reader.jsonl"} <= names
    # Hidden sidecars must not leak into the bundle.
    assert not any(n.startswith(".") for n in names)

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["run"]["run_id"] == "20260704T120000Z"
    present = {a["name"]: a["present"] for a in manifest["artifacts"]}
    assert present["libstp.jsonl"] and present["localization.jsonl"]
    assert present["profile.json"] and present["run.json"]
    # Journal files are recorded as artifacts too (full inventory).
    assert present["journal.raccoon-server.jsonl"] is True
    assert manifest["cmd_trace"]["available"] is False

    journals = {j["unit"]: j for j in manifest["journals"]}
    assert journals["raccoon.service"]["entry_count"] == 1
    assert journals["stm32_data_reader.service"]["available"] is False
    # The manifest section must not carry the raw entries.
    assert "entries" not in journals["raccoon.service"]
    # The server-up entry actually landed in the written journal file.
    body = (out / "journal.raccoon-server.jsonl").read_text().strip()
    assert json.loads(body)["message"] == "server up"


def test_download_local_missing_artifacts_marked_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(logs_cmd, "collect_journals", lambda u, s, e: _fake_journals())
    project = tmp_path
    run_dir = _make_run_dir(project)
    (run_dir / "localization.jsonl").unlink()  # opted out of localization
    (run_dir / "profile.json").unlink()
    out = tmp_path / "dl"

    logs_cmd._download_local(
        _fake_ctx(project), MagicMock(), run_id=1,
        output_dir=str(out), pad_secs=2.0,
        cmd_trace_path=str(tmp_path / "nope.jsonl"),
    )
    manifest = json.loads((out / "manifest.json").read_text())
    present = {a["name"]: a["present"] for a in manifest["artifacts"]}
    assert present["libstp.jsonl"] is True
    assert present["localization.jsonl"] is False
    assert present["profile.json"] is False


def test_download_local_errors_when_run_not_found(tmp_path: Path):
    """With no runs at all, download exits rather than bundling nothing."""
    project = tmp_path
    with pytest.raises(SystemExit):
        logs_cmd._download_local(
            _fake_ctx(project), MagicMock(), run_id=1,
            output_dir=str(tmp_path / "dl"), pad_secs=2.0, cmd_trace_path=None,
        )


# ── server zip → client extract round-trip ─────────────────────────


def _empty_cmd_trace() -> dict:
    return {
        "path": "/nope/cmd_trace.jsonl", "available": False,
        "total_lines": 0, "matched_lines": 0,
        "window_start_us": 0, "window_end_us": 1, "pad_secs": 2.0, "entries": [],
    }


def test_server_zip_roundtrips_to_client(tmp_path: Path, monkeypatch):
    project = tmp_path
    run_dir = _make_run_dir(project)
    files = discover_log_files(project / ".raccoon" / "runs")
    run = load_run_by_index(files, 1)
    assert run is not None and run.run_dir

    zip_bytes = server_logs._build_run_bundle_zip(
        run, Path(run.run_dir), _empty_cmd_trace(), _fake_journals()
    )

    # The zip carries every artifact + cmd_trace + journals + manifest, no
    # hidden sidecars.
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        manifest_in_zip = json.loads(zf.read("manifest.json"))
    assert {"libstp.jsonl", "localization.jsonl", "profile.json", "run.json",
            "cmd_trace.jsonl", "manifest.json",
            "journal.raccoon-server.jsonl",
            "journal.stm32-data-reader.jsonl"} <= names
    assert not any(n.startswith(".") for n in names)
    assert {j["unit"] for j in manifest_in_zip["journals"]} == {
        "raccoon.service", "stm32_data_reader.service"
    }

    # Drive the client extraction path with a fake API client.
    out = tmp_path / "remote_dl"

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def download_log_bundle(self, project_uuid, run_index, pad_secs):
            return zip_bytes

    monkeypatch.setattr(
        "raccoon_cli.client.api.create_api_client", lambda *a, **k: _FakeClient()
    )
    state = SimpleNamespace(
        pi_address="1.2.3.4", pi_port=8421, api_token="t", pi_hostname="pi"
    )
    asyncio.run(
        logs_cmd._download_remote(
            MagicMock(), (state, "uuid"), run_id=1,
            output_dir=str(out), pad_secs=2.0,
        )
    )
    extracted = {p.name for p in out.iterdir() if p.is_file()}
    assert {"libstp.jsonl", "localization.jsonl", "profile.json", "run.json",
            "manifest.json", "cmd_trace.jsonl",
            "journal.raccoon-server.jsonl",
            "journal.stm32-data-reader.jsonl"} <= extracted
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["run"]["run_id"] == "20260704T120000Z"
    assert len(manifest["journals"]) == 2
