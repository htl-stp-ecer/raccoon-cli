"""Tests for the per-run summary sidecar cache (``logs.run_cache``).

The cache lets ``raccoon logs`` list runs without re-parsing multi-MB files on
every call. These tests pin the behaviour that makes that safe: a cache hit
matches a full parse, the entries are *not* loaded on a hit, and a file that
changes on disk invalidates its own cache.
"""

from pathlib import Path
from textwrap import dedent

from raccoon_cli.logs import discover_log_files, load_runs
from raccoon_cli.logs import run_cache


def _make_logs_dir(root: Path) -> Path:
    log_dir = root / ".raccoon" / "logs"
    log_dir.mkdir(parents=True)
    return log_dir


def _run_body(started: str) -> str:
    return dedent(
        f"""\
        {started} |     0.000s | info     |                                | Logging to directory: /logs
        {started} |     0.001s | info     | p.Motor.cpp                    | Motor init
        {started} |     0.500s | warning  | test.cpp                       | low battery
        {started} |     1.250s | info     | p.Motor.cpp                    | done
        """
    )


class TestRunCache:
    def test_cold_writes_sidecar_and_warm_matches(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        f = log_dir / "libstp-2026-07-01_10-00-00.log"
        f.write_text(_run_body("2026-07-01 10:00:00"))
        files = discover_log_files(log_dir, include_legacy=False)

        # Cold: parses and writes the sidecar.
        cold = load_runs(files)
        assert run_cache.cache_path(f).exists()
        assert len(cold) == 1
        cold_run = cold[0]
        assert cold_run.line_count == 4
        assert cold_run.level_counts == {"INFO": 3, "WARN": 1}
        assert cold_run.sources == {"p.Motor.cpp", "test.cpp"}

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
        log_dir = _make_logs_dir(tmp_path)
        f = log_dir / "libstp-2026-07-01_10-00-00.log"
        f.write_text(_run_body("2026-07-01 10:00:00"))
        files = discover_log_files(log_dir, include_legacy=False)

        first = load_runs(files)[0]
        assert first.line_count == 4

        # Append another line (size + mtime change) — the stale sidecar must be
        # ignored and the file re-parsed.
        with f.open("a", encoding="utf-8") as fh:
            fh.write("2026-07-01 10:00:00 |     2.000s | error    | x.cpp                          | boom\n")

        second = load_runs(files)[0]
        assert second.line_count == 5
        assert second.level_counts.get("ERROR") == 1

    def test_corrupt_sidecar_falls_back_to_parse(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        f = log_dir / "libstp-2026-07-01_10-00-00.log"
        f.write_text(_run_body("2026-07-01 10:00:00"))

        # Garbage sidecar must not crash the loader — it should parse instead.
        run_cache.cache_path(f).write_text("{ not json")

        files = discover_log_files(log_dir, include_legacy=False)
        runs = load_runs(files)
        assert len(runs) == 1
        assert runs[0].line_count == 4

    def test_sidecar_is_hidden_and_not_discovered(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        f = log_dir / "libstp-2026-07-01_10-00-00.log"
        f.write_text(_run_body("2026-07-01 10:00:00"))
        load_runs(discover_log_files(log_dir, include_legacy=False))

        # The sidecar exists but must never be picked up as a run file.
        assert run_cache.cache_path(f).exists()
        discovered = [p.name for p in discover_log_files(log_dir, include_legacy=False)]
        assert discovered == ["libstp-2026-07-01_10-00-00.log"]
