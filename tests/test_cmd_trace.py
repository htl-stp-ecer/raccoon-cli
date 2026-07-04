"""Tests for cmd_trace.jsonl reading and log-run windowing."""

import json
from datetime import datetime
from pathlib import Path

from raccoon_cli.logs.cmd_trace import (
    DEFAULT_CMD_TRACE_PATH,
    datetime_to_us,
    load_cmd_trace,
    resolve_cmd_trace_path,
    run_window_us,
    slice_cmd_trace,
)


def _rec(w_us: int, **extra) -> dict:
    base = {
        "t_ns": w_us * 1000,
        "w_us": w_us,
        "rseq": 0,
        "stage": "recv",
        "kind": "servo_pos",
        "ch": "raccoon/servo/0/position_cmd",
        "port": 0,
        "v": 30.0,
        "ts_us": 0,
    }
    base.update(extra)
    return base


class TestLoadCmdTrace:
    def test_parses_valid_lines(self, tmp_path: Path):
        p = tmp_path / "cmd_trace.jsonl"
        p.write_text("".join(json.dumps(_rec(w)) + "\n" for w in (1000, 2000, 3000)))
        records = load_cmd_trace(p)
        assert [r["w_us"] for r in records] == [1000, 2000, 3000]

    def test_skips_blank_and_malformed_lines(self, tmp_path: Path):
        # A trailing partial line (crash mid-flush) and a blank line must not
        # break the whole read.
        p = tmp_path / "cmd_trace.jsonl"
        p.write_text(
            json.dumps(_rec(1000)) + "\n"
            + "\n"
            + json.dumps(_rec(2000)) + "\n"
            + '{"w_us":3000,"stage":'  # truncated, no newline
        )
        records = load_cmd_trace(p)
        assert [r["w_us"] for r in records] == [1000, 2000]


class TestWindowing:
    def test_datetime_to_us_round_trips_local(self):
        dt = datetime(2026, 7, 1, 14, 30, 0)
        assert datetime_to_us(dt) == int(dt.timestamp() * 1_000_000)

    def test_run_window_pads_both_ends(self):
        start = datetime(2026, 7, 1, 14, 30, 0)
        end = datetime(2026, 7, 1, 14, 30, 10)
        s_us, e_us = run_window_us(start, end, pad_secs=2.0)
        assert s_us == datetime_to_us(start) - 2_000_000
        assert e_us == datetime_to_us(end) + 2_000_000

    def test_slice_keeps_only_in_window(self):
        records = [_rec(w) for w in (500, 1000, 1500, 2000, 2500)]
        matched = slice_cmd_trace(records, 1000, 2000)
        assert [r["w_us"] for r in matched] == [1000, 1500, 2000]

    def test_slice_ignores_records_without_w_us(self):
        records = [_rec(1500), {"stage": "spi", "kind": "servo_pos"}]
        matched = slice_cmd_trace(records, 1000, 2000)
        assert len(matched) == 1

    def test_slice_matches_run_timeframe(self):
        # A run from 14:30:00 to 14:30:10; commands just inside/outside the
        # padded window are kept/dropped accordingly.
        start = datetime(2026, 7, 1, 14, 30, 0)
        end = datetime(2026, 7, 1, 14, 30, 10)
        s_us, e_us = run_window_us(start, end, pad_secs=1.0)
        during = datetime_to_us(datetime(2026, 7, 1, 14, 30, 5))
        way_before = datetime_to_us(datetime(2026, 7, 1, 14, 0, 0))
        records = [_rec(during), _rec(way_before)]
        matched = slice_cmd_trace(records, s_us, e_us)
        assert [r["w_us"] for r in matched] == [during]


class TestResolvePath:
    def test_falls_back_to_default_when_systemctl_missing(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise FileNotFoundError("systemctl not found")

        monkeypatch.setattr("subprocess.run", _boom)
        assert resolve_cmd_trace_path() == DEFAULT_CMD_TRACE_PATH

    def test_reads_env_from_systemctl(self, monkeypatch):
        class _Proc:
            stdout = "Environment=WOMBAT_CMD_TRACE=/custom/trace.jsonl FOO=bar\n"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
        assert resolve_cmd_trace_path() == Path("/custom/trace.jsonl")

    def test_falls_back_when_env_lacks_var(self, monkeypatch):
        class _Proc:
            stdout = "Environment=FOO=bar\n"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
        assert resolve_cmd_trace_path() == DEFAULT_CMD_TRACE_PATH
