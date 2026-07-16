"""Tests for turning a crashed run's stderr into JSONL log records.

A Python runtime error prints a traceback to stderr, which the JSONL run log and
the live stream never see. :mod:`raccoon_cli.logs.crash` converts it into ERROR
records that (a) parse back cleanly via the shared JSONL parser and (b) can be
appended to the run's ``libstp.jsonl``. These tests pin that round trip.
"""

import json
from pathlib import Path

from raccoon_cli.logs.crash import append_crash_records, build_crash_records
from raccoon_cli.logs.parser import parse_jsonl_line, parse_log_file

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "src/main.py", line 12, in <module>\n'
    "    robot.run()\n"
    '  File "src/hardware/robot.py", line 44, in run\n'
    "    self.motor.drive()\n"
    "ZeroDivisionError: division by zero\n"
)


class TestBuildCrashRecords:
    def test_one_record_per_nonblank_line(self):
        records = build_crash_records(TRACEBACK)
        # 6 meaningful lines, blank trailing line dropped.
        assert len(records) == 6

    def test_records_parse_as_error_jsonl(self):
        records = build_crash_records(TRACEBACK, elapsed=1.5, pid=4321)
        entries = [parse_jsonl_line(r) for r in records]
        assert all(e is not None for e in entries)
        assert all(e.level_upper == "ERROR" for e in entries)
        assert entries[0].pid == 4321
        assert entries[0].elapsed == 1.5
        # The exception line survives verbatim as the final record's message.
        assert entries[-1].message == "ZeroDivisionError: division by zero"

    def test_sequences_are_monotonic_and_offset(self):
        records = build_crash_records(TRACEBACK, seq_start=1_000_000)
        seqs = [json.loads(r)["seq"] for r in records]
        assert seqs == list(range(1_000_000, 1_000_000 + len(records)))

    def test_blank_input_yields_nothing(self):
        assert build_crash_records("") == []
        assert build_crash_records("   \n  \n") == []

    def test_runaway_stderr_is_capped_with_notice(self):
        huge = "\n".join(f"line {i}" for i in range(500))
        records = build_crash_records(huge, max_lines=10)
        # 10 kept lines + 1 inserted "omitted" notice.
        assert len(records) == 11
        first = parse_jsonl_line(records[0])
        assert "omitted" in first.message
        # The tail (most diagnostic frames) is what's kept.
        assert parse_jsonl_line(records[-1]).message == "line 499"


class TestAppendCrashRecords:
    def test_appends_to_existing_log_and_reparses(self, tmp_path: Path):
        log = tmp_path / "libstp.jsonl"
        real = {
            "t": "2026-07-14T10:00:00", "elapsed": 0.5, "seq": 1,
            "level": "info", "msg": "running", "file": "base.py", "line": 3,
        }
        log.write_text(json.dumps(real) + "\n", encoding="utf-8")

        records = build_crash_records(TRACEBACK, elapsed=2.0)
        append_crash_records(log, records)

        entries = parse_log_file(log)
        assert entries[0].message == "running"
        assert entries[-1].message == "ZeroDivisionError: division by zero"
        assert [e for e in entries if e.level_upper == "ERROR"]

    def test_creates_file_when_child_crashed_before_any_log(self, tmp_path: Path):
        log = tmp_path / "runs" / "r1" / "libstp.jsonl"
        records = build_crash_records(TRACEBACK)
        append_crash_records(log, records)
        assert log.exists()
        assert len(parse_log_file(log)) == len(records)

    def test_partial_last_line_gets_a_newline_separator(self, tmp_path: Path):
        log = tmp_path / "libstp.jsonl"
        # A library flush interrupted mid-line: no trailing newline.
        real = {"t": "2026-07-14T10:00:00", "level": "info", "msg": "partial"}
        log.write_text(json.dumps(real), encoding="utf-8")  # no "\n"

        append_crash_records(log, build_crash_records("boom\n"))

        # Both the partial real record and the crash record survive as separate,
        # parseable lines (they weren't glued together).
        entries = parse_log_file(log)
        assert entries[0].message == "partial"
        assert entries[-1].message == "boom"

    def test_empty_records_is_a_noop(self, tmp_path: Path):
        log = tmp_path / "libstp.jsonl"
        append_crash_records(log, [])
        assert not log.exists()
