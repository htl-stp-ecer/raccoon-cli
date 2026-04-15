"""Tests for the log parser and run detection."""

from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest

from raccoon_cli.logs.parser import LogEntry, parse_log_line, detect_runs, parse_log_file


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
