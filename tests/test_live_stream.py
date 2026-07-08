"""Tests for the live JSONL run-log streaming TUI (raccoon_cli.logs.live_stream)."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

from rich.console import Console

from raccoon_cli.logs.live_stream import (
    LiveLogView,
    LiveRecord,
    follow_lines,
    newest_jsonl,
    parse_record,
    stream_run_logs,
    wait_for_new_jsonl,
)


def _rec(**kw) -> str:
    base = {
        "t": "2026-07-04T15:22:01.693",
        "elapsed": 1.5,
        "seq": 0,
        "level": "info",
        "logger": "core",
        "thread": 1,
        "pid": 1,
        "file": "/repo/python/raccoon/step/base.py",
        "line": 91,
        "func": "DriveStep._execute_step",
        "msg": "hello",
    }
    base.update(kw)
    return json.dumps(base)


# ── parse_record ───────────────────────────────────────────────────


def test_parse_record_maps_all_fields():
    r = parse_record(_rec(msg='say "hi"\n', level="warning", line=42, seq=7))
    assert isinstance(r, LiveRecord)
    assert r.elapsed == 1.5
    assert r.level == "WARN"  # WARNING normalised
    assert r.file == "base.py"  # basename only
    assert r.line == 42
    assert r.func == "DriveStep._execute_step"
    assert r.message == 'say "hi"\n'
    assert r.seq == 7
    assert r.source == "base.py:42"


def test_parse_record_skips_blank_and_non_json():
    assert parse_record("") is None
    assert parse_record("   ") is None
    assert parse_record("not json at all") is None
    assert parse_record("[1,2,3]") is None  # JSON, but not an object


def test_parse_record_tolerates_missing_and_bad_fields():
    r = parse_record(json.dumps({"msg": "x"}))
    assert r is not None
    assert r.elapsed == 0.0 and r.line == 0 and r.level == "" and r.file == ""
    # bad numeric types fall back to defaults, don't raise
    r2 = parse_record(json.dumps({"elapsed": "oops", "line": None, "msg": "y"}))
    assert r2 is not None and r2.elapsed == 0.0 and r2.line == 0


# ── newest_jsonl / wait_for_new_jsonl ──────────────────────────────


def test_newest_jsonl_picks_newest_run_dir(tmp_path: Path):
    runs = tmp_path / ".raccoon" / "runs"
    assert newest_jsonl(runs) is None
    for rid in ("20260704T100000Z", "20260704T120000Z"):
        (runs / rid).mkdir(parents=True)
        (runs / rid / "libstp.jsonl").write_text("{}\n")
    (runs / "junk").mkdir()  # invalid run_id — ignored
    got = newest_jsonl(runs)
    assert got is not None and got.parent.name == "20260704T120000Z"


def test_wait_for_new_jsonl_excludes_existing(tmp_path: Path):
    old = tmp_path / "libstp-2026-07-04_10-00-00.jsonl"
    old.write_text("")
    new = tmp_path / "libstp-2026-07-04_12-00-00.jsonl"

    def create_soon():
        time.sleep(0.15)
        new.write_text("")

    threading.Thread(target=create_soon, daemon=True).start()
    got = wait_for_new_jsonl(
        tmp_path, exclude={old}, should_continue=lambda: True, timeout=2.0, poll=0.02
    )
    assert got is not None and got.name == new.name


def test_wait_for_new_jsonl_returns_none_if_process_dies(tmp_path: Path):
    got = wait_for_new_jsonl(
        tmp_path, exclude=set(), should_continue=lambda: False, timeout=2.0, poll=0.02
    )
    assert got is None


# ── follow_lines (tail -f) ─────────────────────────────────────────


def test_follow_lines_tails_growing_file(tmp_path: Path):
    path = tmp_path / "libstp-run.jsonl"
    path.write_text("line1\nline2\n")
    stop = threading.Event()
    out: list[str] = []

    def writer():
        time.sleep(0.1)
        with open(path, "a") as f:
            f.write("line3\n")
            f.flush()
        time.sleep(0.1)
        with open(path, "a") as f:
            f.write("partial-no-newline")  # unterminated final line
            f.flush()
        time.sleep(0.1)
        stop.set()

    threading.Thread(target=writer, daemon=True).start()
    for ln in follow_lines(path, should_stop=stop.is_set, poll=0.02):
        out.append(ln)

    assert out == ["line1", "line2", "line3", "partial-no-newline"]


def test_follow_lines_stops_immediately_when_already_stopped(tmp_path: Path):
    path = tmp_path / "libstp-run.jsonl"
    path.write_text("only\n")
    out = list(follow_lines(path, should_stop=lambda: True, poll=0.02))
    assert out == ["only"]


# ── LiveLogView ────────────────────────────────────────────────────


def _headless_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True, width=120, height=30)


def test_view_push_updates_counts_and_renders():
    view = LiveLogView(_headless_console(), title="proj")
    view.push(parse_record(_rec(level="info")))
    view.push(parse_record(_rec(level="warning")))
    view.push(parse_record(_rec(level="error")))
    assert view.counts["INFO"] == 1
    assert view.counts["WARN"] == 1
    assert view.counts["ERROR"] == 1
    assert view.warn_error_count == 2
    assert view.total == 3
    # render must not raise and must produce output
    con = _headless_console()
    con.print(view.render())
    assert con.file.getvalue()  # something was rendered


def test_view_hides_trace_and_debug_from_body_but_counts_them():
    view = LiveLogView(_headless_console(), title="proj")
    view.push(parse_record(_rec(level="trace", msg="t")))
    view.push(parse_record(_rec(level="debug", msg="d")))
    view.push(parse_record(_rec(level="info", msg="i")))
    view.push(parse_record(_rec(level="warning", msg="w")))
    # Body holds only INFO and above…
    shown = [r.message for r in view.records]
    assert shown == ["i", "w"]
    # …but counts and total still reflect every record.
    assert view.total == 4
    assert view.counts["TRACE"] == 1
    assert view.counts["DEBUG"] == 1


def test_view_breadcrumb_uses_hidden_debug_records():
    """Debug-level mission-preload markers build the breadcrumb even though the
    body hides debug lines."""
    view = LiveLogView(_headless_console(), title="proj")
    view.push(parse_record(_rec(level="debug", msg="Preloading main mission: M010Foo")))
    view.push(parse_record(_rec(level="debug", msg="Preloading main mission: M020Bar")))
    view.push(parse_record(_rec(level="info", msg="Starting mission: M020Bar")))
    view.push(parse_record(_rec(level="info", msg="3/8: DriveForward")))
    body = [r.message for r in view.records]
    assert body == ["Starting mission: M020Bar", "3/8: DriveForward"]  # no debug lines
    assert view.progress.breadcrumb() == "main · M020Bar (2/2) · 3/8 DriveForward"


def test_view_min_level_override_shows_debug():
    view = LiveLogView(_headless_console(), title="proj", min_level="DEBUG")
    view.push(parse_record(_rec(level="trace", msg="t")))
    view.push(parse_record(_rec(level="debug", msg="d")))
    assert [r.message for r in view.records] == ["d"]


def test_view_body_caps_to_visible_rows():
    view = LiveLogView(_headless_console(), title="proj")
    for i in range(500):
        view.push(parse_record(_rec(seq=i, msg=f"m{i}")))
    # rendering a huge backlog stays bounded (no crash, no unbounded row count)
    con = _headless_console()
    con.print(view.render())
    assert view.total == 500


# ── stream_run_logs end-to-end ─────────────────────────────────────


def test_stream_run_logs_streams_a_file(tmp_path: Path):
    # No explicit log_path → exercises the wait-for-new-file discovery fallback.
    log_dir = tmp_path
    logf = log_dir / "libstp-2026-07-04_12-00-00.jsonl"
    lines = [_rec(seq=i, msg=f"record {i}", level="info") for i in range(5)]
    lines.append(_rec(seq=5, msg="a warning", level="warning"))
    logf.write_text("\n".join(lines) + "\n")

    # process "runs" for two polls, then exits
    state = {"n": 0}

    def is_running():
        state["n"] += 1
        return state["n"] < 2

    con = _headless_console()
    ok = stream_run_logs(
        log_dir, is_running=is_running, console=con, title="proj", existing=set()
    )
    assert ok is True


def test_stream_run_logs_tails_explicit_path(tmp_path: Path):
    """When given an exact log path, the streamer tails it (no discovery race)."""
    log_dir = tmp_path / ".raccoon" / "runs" / "20260704T120000Z"
    log_dir.mkdir(parents=True)
    logf = log_dir / "libstp.jsonl"
    logf.write_text("\n".join(_rec(seq=i, msg=f"r{i}") for i in range(3)) + "\n")

    state = {"n": 0}

    def is_running():
        state["n"] += 1
        return state["n"] < 2

    ok = stream_run_logs(
        tmp_path / ".raccoon" / "runs",
        is_running=is_running,
        console=_headless_console(),
        title="proj",
        log_path=logf,
    )
    assert ok is True


def test_stream_run_logs_explicit_path_missing_returns_false(tmp_path: Path):
    ok = stream_run_logs(
        tmp_path / ".raccoon" / "runs",
        is_running=lambda: False,
        console=_headless_console(),
        title="proj",
        log_path=tmp_path / "never" / "libstp.jsonl",
        startup_timeout=0.2,
    )
    assert ok is False


def test_stream_run_logs_returns_false_without_file(tmp_path: Path):
    log_dir = tmp_path
    con = _headless_console()
    ok = stream_run_logs(
        log_dir,
        is_running=lambda: False,
        console=con,
        title="proj",
        existing=set(),
        startup_timeout=0.3,
    )
    assert ok is False
