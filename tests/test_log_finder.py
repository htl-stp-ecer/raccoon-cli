"""Tests for log-file discovery (per-run dated files + legacy fallback)."""

from pathlib import Path

from raccoon_cli.logs import current_log_file, discover_log_files, find_log_dir
from raccoon_cli.logs.finder import _is_log_dir


def _make_logs_dir(root: Path) -> Path:
    log_dir = root / ".raccoon" / "logs"
    log_dir.mkdir(parents=True)
    return log_dir


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
