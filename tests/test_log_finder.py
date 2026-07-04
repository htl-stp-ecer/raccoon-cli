"""Tests for run discovery under ``.raccoon/runs/`` (one JSONL log per run dir)."""

import json
from pathlib import Path

from raccoon_cli.logs import (
    current_log_file,
    discover_log_files,
    find_log_dir,
    is_run_file,
    load_run_by_index,
    load_runs,
)
from raccoon_cli.logs import finder as finder_mod
from raccoon_cli.logs.finder import _is_log_dir


def _make_runs_dir(root: Path) -> Path:
    runs_dir = root / ".raccoon" / "runs"
    runs_dir.mkdir(parents=True)
    return runs_dir


def _jsonl_body(iso_prefix: str) -> str:
    """A minimal per-run JSONL body: a couple of records with real fields."""
    lines = [
        {"t": f"{iso_prefix}.000", "elapsed": 0.0, "seq": 0, "level": "info",
         "file": "/x/api.py", "line": 10, "func": "Robot.start", "msg": "start"},
        {"t": f"{iso_prefix}.500", "elapsed": 0.5, "seq": 1, "level": "warning",
         "file": "/x/motor.py", "line": 22, "func": "Motor.set", "msg": "low battery"},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _run_body(iso_prefix: str) -> str:
    """A 3-record per-run JSONL body (INFO, INFO, WARN)."""
    lines = [
        {"t": f"{iso_prefix}.000", "elapsed": 0.0, "seq": 0, "level": "info",
         "file": "/x/api.py", "line": 1, "func": "Robot.start", "msg": "start"},
        {"t": f"{iso_prefix}.001", "elapsed": 0.001, "seq": 1, "level": "info",
         "file": "/x/motor.py", "line": 2, "func": "Motor.init", "msg": "init"},
        {"t": f"{iso_prefix}.500", "elapsed": 0.5, "seq": 2, "level": "warning",
         "file": "/x/test.py", "line": 3, "func": "t", "msg": "low battery"},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _make_run(root: Path, run_id: str, iso_prefix: str, *, body=None) -> Path:
    """Create a ``.raccoon/runs/<run_id>/`` dir with a libstp.jsonl log."""
    run_dir = root / ".raccoon" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "libstp.jsonl").write_text((body or _jsonl_body)(iso_prefix))
    (run_dir / "localization.jsonl").write_text('{"t_ns":0}\n')
    (run_dir / "run.json").write_text(f'{{"run_id":"{run_id}"}}\n')
    return run_dir


def _runs_dir(root: Path) -> Path:
    return root / ".raccoon" / "runs"


class TestDiscover:
    def test_run_dirs_sorted_chronologically(self, tmp_path: Path):
        # Created out of order; must come back oldest → newest by run_id.
        _make_run(tmp_path, "20260701T100000Z", "2026-07-01T10:00:00")
        _make_run(tmp_path, "20260629T235959Z", "2026-06-29T23:59:59")
        _make_run(tmp_path, "20260701T090000Z", "2026-07-01T09:00:00")

        assert [p.parent.name for p in discover_log_files(_runs_dir(tmp_path))] == [
            "20260629T235959Z",
            "20260701T090000Z",
            "20260701T100000Z",
        ]

    def test_current_is_newest(self, tmp_path: Path):
        _make_run(tmp_path, "20260629T000000Z", "2026-06-29T00:00:00")
        _make_run(tmp_path, "20260701T120000Z", "2026-07-01T12:00:00")
        cur = current_log_file(_runs_dir(tmp_path))
        assert cur is not None and cur.parent.name == "20260701T120000Z"

    def test_current_none_when_empty(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert current_log_file(empty) is None
        assert discover_log_files(empty) == []

    def test_missing_runs_dir_is_empty(self, tmp_path: Path):
        # No .raccoon/runs at all.
        assert discover_log_files(tmp_path / ".raccoon" / "runs") == []

    def test_run_dir_without_log_is_skipped(self, tmp_path: Path):
        run_dir = tmp_path / ".raccoon" / "runs" / "20260704T130000Z"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("{}\n")  # manifest but no libstp.jsonl
        assert discover_log_files(_runs_dir(tmp_path)) == []

    def test_invalid_run_id_dir_ignored(self, tmp_path: Path):
        bad = tmp_path / ".raccoon" / "runs" / "not-a-run-id"
        bad.mkdir(parents=True)
        (bad / "libstp.jsonl").write_text(_jsonl_body("2026-07-04T13:00:00"))
        assert discover_log_files(_runs_dir(tmp_path)) == []


class TestIsRunFile:
    def test_run_dir_logs_are_run_files(self):
        assert is_run_file(Path("/p/.raccoon/runs/20260704T100000Z/libstp.jsonl"))

    def test_other_files_are_not_run_files(self):
        assert not is_run_file(Path("libstp.log"))
        assert not is_run_file(Path("libstp-2026-07-01_10-00-00.jsonl"))
        assert not is_run_file(Path("/p/.raccoon/runs/badid/libstp.jsonl"))


class TestLoadRuns:
    def test_each_run_dir_is_one_run(self, tmp_path: Path):
        _make_run(tmp_path, "20260701T090000Z", "2026-07-01T09:00:00", body=_run_body)
        _make_run(tmp_path, "20260701T100000Z", "2026-07-01T10:00:00", body=_run_body)

        runs = load_runs(discover_log_files(_runs_dir(tmp_path)))
        assert len(runs) == 2
        assert runs[-1].index == 1  # newest
        assert runs[0].index == 2
        for run in runs:
            assert run.line_count == 3
            assert run.run_id is not None

    def test_run_dir_annotated_and_summarised(self, tmp_path: Path):
        _make_run(tmp_path, "20260704T090000Z", "2026-07-04T09:00:00")
        _make_run(tmp_path, "20260704T130000Z", "2026-07-04T13:00:00")

        files = discover_log_files(_runs_dir(tmp_path))
        assert all(is_run_file(f) for f in files)
        newest = load_run_by_index(files, 1)
        assert newest is not None
        assert newest.index == 1
        assert newest.run_id == "20260704T130000Z"
        assert newest.run_dir == str(tmp_path / ".raccoon" / "runs" / "20260704T130000Z")
        assert newest.line_count == 2
        assert newest.sources == {"api.py", "motor.py"}
        assert newest.level_counts.get("WARN") == 1

    def test_empty(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert load_runs(discover_log_files(empty)) == []


def _make_n_runs(root: Path, n: int) -> None:
    """Create *n* run dirs with chronologically-sortable run_ids (10:00 … )."""
    for i in range(n):
        rid = f"20260701T{10 + i:02d}0000Z"
        _make_run(root, rid, f"2026-07-01T{10 + i:02d}:00:00", body=_run_body)


class TestLoadRunsLimit:
    def test_limit_parses_only_newest_runs(self, tmp_path: Path, monkeypatch):
        _make_n_runs(tmp_path, 5)

        parsed: list[str] = []
        real_parse = finder_mod.parse_log_file

        def _spy(path):
            parsed.append(Path(path).parent.name)
            return real_parse(path)

        monkeypatch.setattr(finder_mod, "parse_log_file", _spy)

        runs = load_runs(discover_log_files(_runs_dir(tmp_path)), limit=2)
        assert parsed == ["20260701T130000Z", "20260701T140000Z"]
        assert [r.index for r in sorted(runs, key=lambda r: r.index)] == [1, 2]

    def test_limit_indices_match_unlimited(self, tmp_path: Path):
        _make_n_runs(tmp_path, 5)
        files = discover_log_files(_runs_dir(tmp_path))
        newest_unlimited = next(r for r in load_runs(files) if r.index == 1)
        newest_limited = next(r for r in load_runs(files, limit=2) if r.index == 1)
        assert newest_limited.run_id == newest_unlimited.run_id

    def test_limit_larger_than_count_is_noop(self, tmp_path: Path):
        _make_n_runs(tmp_path, 3)
        files = discover_log_files(_runs_dir(tmp_path))
        assert len(load_runs(files, limit=99)) == 3


class TestLoadRunByIndex:
    def test_parses_only_the_target_run(self, tmp_path: Path, monkeypatch):
        _make_n_runs(tmp_path, 5)

        parsed: list[str] = []
        real_parse = finder_mod.parse_log_file

        def _spy(path):
            parsed.append(Path(path).parent.name)
            return real_parse(path)

        monkeypatch.setattr(finder_mod, "parse_log_file", _spy)

        run = load_run_by_index(discover_log_files(_runs_dir(tmp_path)), 1)
        assert run is not None and run.index == 1
        assert parsed == ["20260701T140000Z"]  # newest only

    def test_index_maps_from_newest(self, tmp_path: Path):
        _make_n_runs(tmp_path, 5)
        files = discover_log_files(_runs_dir(tmp_path))
        run = load_run_by_index(files, 3)  # 3rd-newest run (12:00)
        assert run is not None and run.run_id == "20260701T120000Z"

    def test_out_of_range_returns_none(self, tmp_path: Path):
        _make_n_runs(tmp_path, 2)
        files = discover_log_files(_runs_dir(tmp_path))
        assert load_run_by_index(files, 0) is None
        assert load_run_by_index(files, 99) is None


class TestFindLogDir:
    def test_finds_runs_dir(self, tmp_path: Path):
        _make_run(tmp_path, "20260701T100000Z", "2026-07-01T10:00:00")
        assert find_log_dir(tmp_path) == _runs_dir(tmp_path)

    def test_walks_up_to_parent(self, tmp_path: Path):
        _make_run(tmp_path, "20260701T100000Z", "2026-07-01T10:00:00")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_log_dir(nested) == _runs_dir(tmp_path)

    def test_empty_runs_dir_is_not_a_log_dir(self, tmp_path: Path):
        empty = _make_runs_dir(tmp_path)
        assert not _is_log_dir(empty)
        assert find_log_dir(tmp_path) is None

    def test_none_without_runs(self, tmp_path: Path):
        # No .raccoon/runs at all.
        assert find_log_dir(tmp_path) is None
