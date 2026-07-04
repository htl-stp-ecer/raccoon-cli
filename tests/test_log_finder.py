"""Tests for log-file discovery (per-run dated files + legacy fallback)."""

from pathlib import Path
from textwrap import dedent

from raccoon_cli.logs import (
    current_log_file,
    discover_log_files,
    find_log_dir,
    is_run_file,
    load_runs,
)
from raccoon_cli.logs.finder import _is_log_dir


def _make_logs_dir(root: Path) -> Path:
    log_dir = root / ".raccoon" / "logs"
    log_dir.mkdir(parents=True)
    return log_dir


def _run_body(started: str) -> str:
    """A minimal per-run log body: run-start marker + a couple of lines."""
    return dedent(
        f"""\
        {started} |     0.000s | info     |                                | Logging to directory: /logs
        {started} |     0.001s | info     | p.Motor.cpp                    | Motor init
        {started} |     0.500s | warning  | test.cpp                       | low battery
        """
    )


class TestDiscoverPerRunFiles:
    def test_sorted_chronologically(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        # Written out of order on disk; must come back oldest → newest.
        for name in (
            "libstp-2026-07-01_10-00-00.log",
            "libstp-2026-06-29_23-59-59.log",
            "libstp-2026-07-01_09-00-00.log",
        ):
            (log_dir / name).write_text("x")

        assert [p.name for p in discover_log_files(log_dir)] == [
            "libstp-2026-06-29_23-59-59.log",
            "libstp-2026-07-01_09-00-00.log",
            "libstp-2026-07-01_10-00-00.log",
        ]

    def test_current_is_newest(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        (log_dir / "libstp-2026-06-29_00-00-00.log").write_text("x")
        (log_dir / "libstp-2026-07-01_12-00-00.log").write_text("x")

        assert current_log_file(log_dir).name == "libstp-2026-07-01_12-00-00.log"

    def test_current_none_when_empty(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert current_log_file(empty) is None
        assert discover_log_files(empty) == []


class TestLegacyFallback:
    def test_legacy_rotation_ordered_oldest_first(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        for name in ("libstp.log", "libstp.1.log", "libstp.2.log"):
            (log_dir / name).write_text("x")

        assert [p.name for p in discover_log_files(log_dir)] == [
            "libstp.2.log",
            "libstp.1.log",
            "libstp.log",
        ]

    def test_legacy_precedes_new_runs(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        (log_dir / "libstp.log").write_text("x")
        (log_dir / "libstp-2026-07-01_10-00-00.log").write_text("x")

        files = discover_log_files(log_dir)
        assert files[0].name == "libstp.log"
        assert files[-1].name == "libstp-2026-07-01_10-00-00.log"
        # Newest overall is still the dated per-run file.
        assert current_log_file(log_dir).name == "libstp-2026-07-01_10-00-00.log"

    def test_include_legacy_false_excludes_rotation_files(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        (log_dir / "libstp.log").write_text("x")
        (log_dir / "libstp.1.log").write_text("x")
        (log_dir / "libstp-2026-07-01_10-00-00.log").write_text("x")

        names = [p.name for p in discover_log_files(log_dir, include_legacy=False)]
        assert names == ["libstp-2026-07-01_10-00-00.log"]


class TestIsRunFile:
    def test_per_run_files(self):
        assert is_run_file(Path("libstp-2026-07-01_10-00-00.log"))
        assert is_run_file(Path("/x/y/libstp-2026-07-01_10-00-00.log"))

    def test_legacy_files_are_not_run_files(self):
        assert not is_run_file(Path("libstp.log"))
        assert not is_run_file(Path("libstp.1.log"))


class TestLoadRuns:
    def test_each_file_is_one_run(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        (log_dir / "libstp-2026-07-01_09-00-00.log").write_text(_run_body("2026-07-01 09:00:00"))
        (log_dir / "libstp-2026-07-01_10-00-00.log").write_text(_run_body("2026-07-01 10:00:00"))

        runs = load_runs(discover_log_files(log_dir))
        assert len(runs) == 2
        # Newest = index 1.
        assert runs[-1].index == 1
        assert runs[0].index == 2
        for run in runs:
            assert run.line_count == 3

    def test_per_run_file_not_split_on_elapsed_reset(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        # Two "Logging to directory" markers + an elapsed reset in ONE file must
        # still be a single run under the new scheme.
        body = dedent(
            """\
            2026-07-01 10:00:00 |     0.000s | info     |                                | Logging to directory: /logs
            2026-07-01 10:00:05 |     5.000s | info     | p.Motor.cpp                    | mid run
            2026-07-01 10:00:06 |     0.000s | info     |                                | Logging to directory: /logs
            2026-07-01 10:00:07 |     1.000s | info     | p.Motor.cpp                    | still same file
            """
        )
        (log_dir / "libstp-2026-07-01_10-00-00.log").write_text(body)

        runs = load_runs(discover_log_files(log_dir))
        assert len(runs) == 1
        assert runs[0].line_count == 4

    def test_legacy_file_still_splits_into_runs(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        body = _run_body("2026-07-01 08:00:00") + _run_body("2026-07-01 09:00:00")
        (log_dir / "libstp.log").write_text(body)

        runs = load_runs(discover_log_files(log_dir))
        assert len(runs) == 2

    def test_empty(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert load_runs(discover_log_files(empty)) == []


class TestFindLogDir:
    def test_finds_dir_with_per_run_file(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        (log_dir / "libstp-2026-07-01_10-00-00.log").write_text("x")
        assert find_log_dir(tmp_path) == log_dir

    def test_walks_up_to_parent(self, tmp_path: Path):
        log_dir = _make_logs_dir(tmp_path)
        (log_dir / "libstp-2026-07-01_10-00-00.log").write_text("x")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_log_dir(nested) == log_dir

    def test_empty_dir_is_not_a_log_dir(self, tmp_path: Path):
        empty = _make_logs_dir(tmp_path)
        assert not _is_log_dir(empty)
        assert find_log_dir(tmp_path) is None
