"""Tests for windowed systemd-journal collection used in run bundles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from raccoon_cli.logs import journal


def _journal_json_line(ts_us: int, priority: str, msg: str) -> str:
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": str(ts_us),
            "PRIORITY": priority,
            "MESSAGE": msg,
            "_PID": "42",
            "SYSLOG_IDENTIFIER": "raccoon",
        }
    )


# ── parsing ─────────────────────────────────────────────────────────


def test_parse_journal_json_maps_priority_and_timestamp():
    stdout = "\n".join(
        [
            _journal_json_line(1_000_000, "3", "boom"),  # ERROR, 1s past epoch
            _journal_json_line(2_000_000, "6", "hello"),  # INFO, 2s past epoch
            "not json — skipped",
            "",
        ]
    )
    entries = journal.parse_journal_json(stdout)
    assert [e["level"] for e in entries] == ["ERROR", "INFO"]
    assert [e["message"] for e in entries] == ["boom", "hello"]
    assert entries[0]["timestamp"].startswith("1970-01-01T00:00:01")
    assert entries[0]["pid"] == "42"


# ── windowing ───────────────────────────────────────────────────────


def test_journalctl_window_runs_since_until(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        out = _journal_json_line(5_000_000, "4", "warn") + "\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(journal.subprocess, "run", fake_run)
    entries, error = journal.journalctl_window("raccoon.service", 0, 10_000_000)

    assert error is None
    assert len(entries) == 1 and entries[0]["level"] == "WARN"
    cmd = captured["cmd"]
    assert cmd[:3] == ["journalctl", "-u", "raccoon.service"]
    assert "--since" in cmd and "--until" in cmd
    assert "-o" in cmd and "json" in cmd


def test_journalctl_window_reports_error_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No journal files")

    monkeypatch.setattr(journal.subprocess, "run", fake_run)
    entries, error = journal.journalctl_window("nope.service", 0, 1)
    assert entries == []
    assert error == "No journal files"


def test_journalctl_window_survives_oserror(monkeypatch):
    def boom(cmd, **kwargs):
        raise OSError("journalctl not found")

    monkeypatch.setattr(journal.subprocess, "run", boom)
    entries, error = journal.journalctl_window("raccoon.service", 0, 1)
    assert entries == []
    assert "journalctl not found" in error


# ── unit selection ──────────────────────────────────────────────────


def test_bundle_journal_units_core_only_without_project():
    units = journal.bundle_journal_units(None)
    assert units == list(journal.CORE_BUNDLE_UNITS)


def test_bundle_journal_units_includes_project_services(tmp_path: Path):
    (tmp_path / "raccoon.project.yml").write_text(
        "uuid: proj123\n"
        "services:\n"
        "  vision:\n"
        "    module: src.daemons.vision\n"
    )
    units = journal.bundle_journal_units(tmp_path)
    labels = [label for label, _ in units]
    systemd = [unit for _, unit in units]
    assert "raccoon-server" in labels and "stm32-data-reader" in labels
    assert "vision" in labels
    assert "raccoon-project-proj123-vision.service" in systemd


def test_bundle_journal_units_dedupes_by_unit(tmp_path: Path):
    # A project can't re-list a core unit; dedup keeps one entry per unit.
    units = journal.bundle_journal_units(tmp_path)  # no config → core only
    assert len(units) == len({u for _, u in units})


# ── filenames + manifest shaping ────────────────────────────────────


def test_journal_filename_sanitises_label():
    assert journal.journal_filename("stm32-data-reader") == "journal.stm32-data-reader.jsonl"
    # Path separators and other unsafe chars are stripped so the name can't
    # escape the bundle dir.
    name = journal.journal_filename("weird/sub name!")
    assert name == "journal.weird-sub-name.jsonl"
    assert "/" not in name and " " not in name


def test_collect_and_manifest_strip_entries(monkeypatch):
    monkeypatch.setattr(
        journal, "journalctl_window", lambda u, s, e: ([{"message": "x"}], None)
    )
    sections = journal.collect_journals([("raccoon-server", "raccoon.service")], 0, 1)
    assert sections[0]["entry_count"] == 1
    assert sections[0]["entries"] == [{"message": "x"}]
    manifest = journal.journal_manifest_section(sections[0])
    assert "entries" not in manifest
    assert manifest["file"] == "journal.raccoon-server.jsonl"


def test_write_journal_file_writes_jsonl(tmp_path: Path):
    section = {
        "file": "journal.raccoon-server.jsonl",
        "entries": [{"message": "a"}, {"message": "b"}],
    }
    size = journal.write_journal_file(tmp_path, section)
    lines = (tmp_path / "journal.raccoon-server.jsonl").read_text().splitlines()
    assert [json.loads(x)["message"] for x in lines] == ["a", "b"]
    assert size > 0
