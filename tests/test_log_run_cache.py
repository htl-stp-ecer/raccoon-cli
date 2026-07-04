"""Tests for the per-run summary sidecar cache (``logs.run_cache``).

The cache lets ``raccoon logs`` list runs without re-parsing multi-MB files on
every call. These tests pin the behaviour that makes that safe: a cache hit
matches a full parse, the entries are *not* loaded on a hit, and a file that
changes on disk invalidates its own cache.
"""

import json
from pathlib import Path

from raccoon_cli.logs import discover_log_files, load_runs
from raccoon_cli.logs import run_cache


def _make_run(root: Path, run_id: str, started: str) -> Path:
    """Create ``.raccoon/runs/<run_id>/libstp.jsonl``; return the log path."""
    run_dir = root / ".raccoon" / "runs" / run_id
    run_dir.mkdir(parents=True)
    f = run_dir / "libstp.jsonl"
    f.write_text(_run_body(started))
    return f


def _runs_dir(root: Path) -> Path:
    return root / ".raccoon" / "runs"


def _run_body(started: str) -> str:
    """A 4-record per-run JSONL body (INFO, INFO, WARN, INFO)."""
    iso = started.replace(" ", "T")
    lines = [
        {"t": f"{iso}.000", "elapsed": 0.0, "level": "info",
         "file": "/x/api.py", "line": 1, "msg": "start"},
        {"t": f"{iso}.001", "elapsed": 0.001, "level": "info",
         "file": "/x/motor.py", "line": 2, "msg": "init"},
        {"t": f"{iso}.500", "elapsed": 0.5, "level": "warning",
         "file": "/x/test.py", "line": 3, "msg": "low battery"},
        {"t": f"{iso}.250", "elapsed": 1.25, "level": "info",
         "file": "/x/motor.py", "line": 4, "msg": "done"},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class TestRunCache:
    def test_cold_writes_sidecar_and_warm_matches(self, tmp_path: Path):
        f = _make_run(tmp_path, "20260701T100000Z", "2026-07-01 10:00:00")
        files = discover_log_files(_runs_dir(tmp_path))

        # Cold: parses and writes the sidecar.
        cold = load_runs(files)
        assert run_cache.cache_path(f).exists()
        assert len(cold) == 1
        cold_run = cold[0]
        assert cold_run.line_count == 4
        assert cold_run.level_counts == {"INFO": 3, "WARN": 1}
        assert cold_run.sources == {"api.py", "motor.py", "test.py"}

        # Warm: served from the sidecar, identical summary, no entries loaded.
        warm = load_runs(files)
        warm_run = warm[0]
        assert warm_run.line_count == cold_run.line_count
        assert warm_run.level_counts == cold_run.level_counts
        assert warm_run.sources == cold_run.sources
        assert warm_run.start_time == cold_run.start_time
        assert warm_run.duration_secs == cold_run.duration_secs
        assert warm_run.entries == []  # the whole point: no re-parse

    def test_growing_file_invalidates_cache(self, tmp_path: Path):
        f = _make_run(tmp_path, "20260701T100000Z", "2026-07-01 10:00:00")
        files = discover_log_files(_runs_dir(tmp_path))

        first = load_runs(files)[0]
        assert first.line_count == 4

        # Append another line (size + mtime change) — the stale sidecar must be
        # ignored and the file re-parsed.
        with f.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"t": "2026-07-01T10:00:02.000", "elapsed": 2.0,
                            "level": "error", "file": "/x/x.py", "line": 5,
                            "msg": "boom"}) + "\n"
            )

        second = load_runs(files)[0]
        assert second.line_count == 5
        assert second.level_counts.get("ERROR") == 1

    def test_corrupt_sidecar_falls_back_to_parse(self, tmp_path: Path):
        f = _make_run(tmp_path, "20260701T100000Z", "2026-07-01 10:00:00")

        # Garbage sidecar must not crash the loader — it should parse instead.
        run_cache.cache_path(f).write_text("{ not json")

        files = discover_log_files(_runs_dir(tmp_path))
        runs = load_runs(files)
        assert len(runs) == 1
        assert runs[0].line_count == 4

    def test_sidecar_is_hidden_and_not_discovered(self, tmp_path: Path):
        f = _make_run(tmp_path, "20260701T100000Z", "2026-07-01 10:00:00")
        load_runs(discover_log_files(_runs_dir(tmp_path)))

        # The sidecar exists but must never be picked up as a run file.
        assert run_cache.cache_path(f).exists()
        discovered = discover_log_files(_runs_dir(tmp_path))
        assert [p.parent.name for p in discovered] == ["20260701T100000Z"]
        assert all(p.name == "libstp.jsonl" for p in discovered)
