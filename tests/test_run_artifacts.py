"""Tests for per-run artifact env injection + run.json manifest in `raccoon run`."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from raccoon_cli.commands.run import _run_local


class FakePopen:
    def __init__(self, *, returncode=0):
        self.pid = 4242
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


@pytest.fixture()
def fake_project(tmp_path):
    (tmp_path / "raccoon.project.yml").write_text("name: proj\nuuid: u\n")
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.fixture()
def click_ctx():
    ctx = MagicMock()
    ctx.obj = {"console": MagicMock()}
    return ctx


@pytest.fixture(autouse=True)
def patch_guards():
    with patch("raccoon_cli.commands.run._active_program_lock", return_value=nullcontext()), \
         patch("raccoon_cli.commands.run._ensure_single_active_program"), \
         patch("raccoon_cli.commands.run._write_active_program_state"), \
         patch("raccoon_cli.commands.run._clear_active_program_state"), \
         patch("raccoon_cli.commands.run._install_termination_handlers", return_value=(None, None)), \
         patch("raccoon_cli.commands.run._restore_termination_handlers"), \
         patch("raccoon_cli.logs.live_stream.stream_run_logs", return_value=True), \
         patch("raccoon_cli.commands.run.sys.stdout.isatty", return_value=True):
        yield


def _run_capture_env(fake_project, click_ctx, **kwargs) -> dict:
    """Run _run_local with a captured Popen; return the env passed to the child."""
    fake = FakePopen(returncode=0)
    with patch("raccoon_cli.commands.run.subprocess.Popen", return_value=fake) as mock_popen, \
         patch("raccoon_cli.commands.run.create_checkpoint"), \
         patch("raccoon_cli.commands.run.create_pipeline"):
        _run_local(
            click_ctx, fake_project, {"name": "proj"}, args=("M050",),
            no_codegen=True, run_id="20260704T120000Z", **kwargs,
        )
    return mock_popen.call_args.kwargs["env"]


def test_defaults_inject_log_localization_and_profile(fake_project, click_ctx):
    env = _run_capture_env(fake_project, click_ctx)  # defaults: record + profile on
    run_dir = fake_project / ".raccoon" / "runs" / "20260704T120000Z"

    assert env["LIBSTP_LOG_DIR"] == str(run_dir)
    assert env["LIBSTP_RECORD_LOCALIZATION"] == "1"
    assert env["LIBSTP_RECORDING_PATH"] == str(run_dir / "localization.jsonl")
    assert env["RACCOON_PROFILE"] == str(run_dir / "profile.json")


def test_run_json_manifest_written(fake_project, click_ctx):
    _run_capture_env(fake_project, click_ctx)
    manifest = json.loads(
        (fake_project / ".raccoon" / "runs" / "20260704T120000Z" / "run.json").read_text()
    )
    assert manifest["run_id"] == "20260704T120000Z"
    assert manifest["project"] == "proj"
    assert manifest["missions"] == ["M050"]
    assert manifest["record_localization"] is True
    assert manifest["profile"] is True
    assert manifest["started_at_utc"] == "2026-07-04T12:00:00Z"
    assert manifest["artifacts"]["log"] == "libstp.jsonl"


def test_no_record_opts_out_of_localization(fake_project, click_ctx):
    env = _run_capture_env(fake_project, click_ctx, record_localization=False)
    assert "LIBSTP_LOG_DIR" in env  # log always on
    assert "LIBSTP_RECORD_LOCALIZATION" not in env
    assert "LIBSTP_RECORDING_PATH" not in env
    assert "RACCOON_PROFILE" in env  # profiling still on

    manifest = json.loads(
        (fake_project / ".raccoon" / "runs" / "20260704T120000Z" / "run.json").read_text()
    )
    assert manifest["record_localization"] is False
    assert manifest["profile"] is True


def test_no_profile_opts_out_of_profiling(fake_project, click_ctx):
    env = _run_capture_env(fake_project, click_ctx, profile=False)
    assert "RACCOON_PROFILE" not in env
    assert env["LIBSTP_RECORD_LOCALIZATION"] == "1"

    manifest = json.loads(
        (fake_project / ".raccoon" / "runs" / "20260704T120000Z" / "run.json").read_text()
    )
    assert manifest["profile"] is False
    assert manifest["record_localization"] is True


def test_record_hz_forwarded(fake_project, click_ctx):
    env = _run_capture_env(fake_project, click_ctx, record_hz=25.0)
    assert env["LIBSTP_RECORDING_HZ"] == "25.0"
