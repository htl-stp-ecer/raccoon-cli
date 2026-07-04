"""Tests for the log parser and run detection."""

import json
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest

from raccoon_cli.logs.parser import (
    LogEntry,
    detect_runs,
    humanize_source,
    parse_jsonl_line,
    parse_log_file,
    parse_log_line,
    single_run,
)


def _jsonl(**kw) -> str:
    base = {
        "t": "2026-07-04T15:32:26.693",
        "elapsed": 0.001,
        "seq": 2,
        "level": "debug",
        "logger": "core",
        "thread": 471302,
        "pid": 471302,
        "file": "/repo/python/raccoon/step/base.py",
        "line": 14,
        "func": "DriveStep._execute_step",
        "msg": "starting drive",
    }
    base.update(kw)
    return json.dumps(base)


class TestParseLogLine:
    def test_basic_line(self):
        line = "2026-04-12 18:15:04 |     3.444s | info     | p.Motor.cpp                    | Mock Motor port=0 setVelocity=9 inverted=false"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.timestamp == datetime(2026, 4, 12, 18, 15, 4)
        assert entry.elapsed == pytest.approx(3.444)
        assert entry.level == "info"
        assert entry.source == "p.Motor.cpp"
        assert entry.message == "Mock Motor port=0 setVelocity=9 inverted=false"

    def test_empty_source(self):
        line = "2026-04-12 21:29:30 |     0.000s | info     |                                | Logging to directory: /some/path"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.source == ""
        assert entry.message == "Logging to directory: /some/path"

    def test_warning_level(self):
        line = "2026-04-12 21:36:34 |     0.002s | warning  | o.stm32_odometry.cpp           | Stm32Odometry::reset"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.level_upper == "WARN"

    def test_invalid_line(self):
        assert parse_log_line("not a log line") is None
        assert parse_log_line("") is None


FIXTURES = Path(__file__).parent / "fixtures"


class TestJsonlFixtureFile:
    """Parse the committed sample .jsonl the way show/tail/download do."""

    def test_sample_file_parses_and_summarises(self):
        entries = parse_log_file(FIXTURES / "libstp-2026-07-04_15-32-26.jsonl")
        assert len(entries) == 5
        assert [e.level_upper for e in entries] == [
            "INFO", "DEBUG", "WARN", "ERROR", "INFO",
        ]
        run = single_run(entries)
        assert run is not None
        assert run.line_count == 5
        assert run.duration_secs == pytest.approx(1.318)
        # Grouped by basename; drive.cpp emitted the warn + error.
        assert run.sources == {"api.py", "base.py", "drive.cpp"}
        assert run.level_counts == {"INFO": 2, "DEBUG": 1, "WARN": 1, "ERROR": 1}
        # Rich location + func survive for a Python record...
        drive = entries[0]
        assert drive.location == "api.py:40"
        assert drive.func == "Robot.start"
        # ...and a C++ record has a source line but no func.
        cpp = entries[2]
        assert cpp.location == "drive.cpp:88"
        assert cpp.func == ""


class TestParseJsonlLine:
    def test_maps_all_fields(self):
        entry = parse_jsonl_line(_jsonl(level="warning", line=42, seq=7))
        assert entry is not None
        assert entry.timestamp == datetime(2026, 7, 4, 15, 32, 26, 693000)
        assert entry.elapsed == pytest.approx(0.001)
        assert entry.level == "warning"
        assert entry.level_upper == "WARN"  # spdlog "warning" normalised
        assert entry.source == "base.py"  # basename only, groupable
        assert entry.source_path == "/repo/python/raccoon/step/base.py"
        assert entry.file_path == ""  # log-file path filled in by the file reader
        assert entry.line_number == 42
        assert entry.func == "DriveStep._execute_step"
        assert entry.message == "starting drive"
        assert entry.seq == 7
        assert entry.thread == 471302
        assert entry.pid == 471302
        assert entry.location == "base.py:42"

    def test_message_preserved_verbatim(self):
        entry = parse_jsonl_line(_jsonl(msg='say "hi"\ttabbed'))
        assert entry is not None
        assert entry.message == 'say "hi"\ttabbed'

    def test_skips_blank_and_non_json(self):
        assert parse_jsonl_line("") is None
        assert parse_jsonl_line("   ") is None
        assert parse_jsonl_line("not json at all") is None
        assert parse_jsonl_line("[1,2,3]") is None  # JSON, but not an object

    def test_tolerates_missing_and_bad_fields(self):
        entry = parse_jsonl_line(json.dumps({"msg": "x"}))
        assert entry is not None
        assert entry.elapsed == 0.0
        assert entry.line_number == 0
        assert entry.level == ""
        assert entry.source == ""
        assert entry.timestamp == datetime(1970, 1, 1)
        assert entry.location == ""
        entry2 = parse_jsonl_line(
            json.dumps({"elapsed": "oops", "line": None, "t": "garbage", "msg": "y"})
        )
        assert entry2 is not None
        assert entry2.elapsed == 0.0
        assert entry2.line_number == 0
        assert entry2.timestamp == datetime(1970, 1, 1)

    def test_windows_path_basename(self):
        entry = parse_jsonl_line(_jsonl(file=r"C:\repo\raccoon\api.py"))
        assert entry is not None
        assert entry.source == "api.py"


class TestHumanizeSource:
    def test_cpp_sources_unchanged(self):
        # Already tidy (parent.file.ext) — left alone.
        assert humanize_source("d.drive.cpp") == "d.drive.cpp"
        assert humanize_source("c.CalibrationStore.cpp") == "c.CalibrationStore.cpp"
        assert humanize_source("h.Digital.cpp") == "h.Digital.cpp"

    def test_python_install_path_trimmed(self):
        # /home/tobias/.venv/lib/python/site-packages/raccoon/robot/api.py
        assert humanize_source("h.t...l.p.s.r.r.api.py") == "r.api.py"
        assert humanize_source("h.t...l.p.s.r.s.base.py") == "s.base.py"
        assert humanize_source("h.t...l.p.s.r.__init__.py") == "r.__init__.py"

    def test_short_and_empty_unchanged(self):
        assert humanize_source("") == ""
        assert humanize_source("api.py") == "api.py"
        assert humanize_source("r.api.py") == "r.api.py"

    def test_applied_during_parse(self):
        line = (
            "2026-07-01 10:12:58 |     0.009s | info     | "
            "h.t...l.p.s.r.r.api.py         | [Robot]: Starting robot"
        )
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.source == "r.api.py"


class TestSingleRun:
    def _entry(self, elapsed: float, message: str = "test") -> LogEntry:
        return LogEntry(
            timestamp=datetime(2026, 7, 1, 10, 0, 0),
            elapsed=elapsed,
            level="info",
            source="test.cpp",
            message=message,
            file_path="/logs/libstp-2026-07-01_10-00-00.log",
        )

    def test_whole_file_is_one_run(self):
        # An elapsed reset mid-file must NOT split a per-run file.
        entries = [
            self._entry(0.0, "Logging to directory: /logs"),
            self._entry(5.0),
            self._entry(0.0, "timer restarted"),
            self._entry(1.0),
        ]
        run = single_run(entries)
        assert run is not None
        assert run.line_count == 4
        assert run.file_path == "/logs/libstp-2026-07-01_10-00-00.log"

    def test_empty_returns_none(self):
        assert single_run([]) is None


class TestDetectRuns:
    def _make_entry(self, elapsed: float, message: str = "test", ts_str: str = "2026-04-12 18:15:04") -> LogEntry:
        return LogEntry(
            timestamp=datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S"),
            elapsed=elapsed,
            level="info",
            source="test.cpp",
            message=message,
        )

    def test_single_run(self):
        entries = [
            self._make_entry(0.0, "Logging to directory: /logs"),
            self._make_entry(1.0),
            self._make_entry(2.0),
        ]
        runs = detect_runs(entries)
        assert len(runs) == 1
        assert runs[0].line_count == 3
        assert runs[0].index == 1

    def test_two_runs(self):
        entries = [
            self._make_entry(0.0, "Logging to directory: /logs"),
            self._make_entry(1.0),
            self._make_entry(5.0),
            self._make_entry(0.0, "Logging to directory: /logs"),
            self._make_entry(1.0),
        ]
        runs = detect_runs(entries)
        assert len(runs) == 2
        # Most recent = index 1
        assert runs[0].index == 2  # first run in chronological order
        assert runs[0].line_count == 3
        assert runs[1].index == 1  # second run = most recent
        assert runs[1].line_count == 2

    def test_empty_entries(self):
        assert detect_runs([]) == []

    def test_elapsed_reset_detection(self):
        entries = [
            self._make_entry(0.0),
            self._make_entry(5.0),
            self._make_entry(10.0),
            # Elapsed resets without "Logging to directory" message
            self._make_entry(0.0, "some other init msg"),
            self._make_entry(1.0),
        ]
        runs = detect_runs(entries)
        assert len(runs) == 2

    def test_duration_from_max_elapsed(self):
        entries = [
            self._make_entry(0.0, "Logging to directory: /logs"),
            self._make_entry(5.0),
            self._make_entry(10.5),
        ]
        runs = detect_runs(entries)
        assert runs[0].duration_secs == pytest.approx(10.5)


class TestParseLogFile:
    def test_parse_file(self, tmp_path: Path):
        log = tmp_path / "libstp.log"
        log.write_text(dedent("""\
            2026-04-12 18:15:04 |     0.000s | info     |                                | Logging to directory: /tmp/logs
            2026-04-12 18:15:04 |     0.001s | info     | p.Motor.cpp                    | Motor init
            2026-04-12 18:15:04 |     0.002s | warning  | test.cpp                       | low battery
        """))
        entries = parse_log_file(log)
        assert len(entries) == 3
        assert entries[2].level_upper == "WARN"

    def test_concatenated_lines(self, tmp_path: Path):
        """Lines can be concatenated at rotation boundaries."""
        log = tmp_path / "libstp.log"
        log.write_text(
            "2026-04-12 18:15:04 |     3.000s | info     | p.Motor.cpp                    | end of run"
            "2026-04-12 18:15:05 |     0.000s | info     |                                | Logging to directory: /tmp"
        )
        entries = parse_log_file(log)
        assert len(entries) == 2

    def test_parse_jsonl_file_dispatch(self, tmp_path: Path):
        log = tmp_path / "libstp-2026-07-04_15-32-26.jsonl"
        log.write_text(
            _jsonl(seq=0, level="info", msg="init")
            + "\n"
            + _jsonl(seq=1, level="warning", msg="low battery")
            + "\n"
            + "\n"  # blank line tolerated
            + "garbage-not-json\n"  # skipped, not crashed
            + _jsonl(seq=2, level="error", msg="boom")
            + "\n"
        )
        entries = parse_log_file(log)
        assert len(entries) == 3
        assert [e.level_upper for e in entries] == ["INFO", "WARN", "ERROR"]
        assert entries[1].message == "low battery"
        # A JSONL entry with no absolute file falls back to the log path.
        bare = tmp_path / "libstp-2026-07-04_16-00-00.jsonl"
        bare.write_text(json.dumps({"msg": "x", "level": "info"}) + "\n")
        got = parse_log_file(bare)
        assert len(got) == 1
        assert got[0].file_path == str(bare)
