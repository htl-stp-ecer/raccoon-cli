"""Tests for the RunRepository service.

These tests cover listing, metadata, deletion, path-traversal hardening,
and the last-line / frame-count reading logic. We materialize fake
project directories on disk with a minimal ``raccoon.project.yml`` so the
repository can resolve uuid -> project path the same way it does in
production.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from raccoon_cli.ide.repositories.run_repository import (
    RunRepository,
    _read_last_line,
)


PROJECT_UUID = "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"


def _make_project(tmp_path: Path, project_name: str = "Demo Bot") -> Path:
    project_dir = tmp_path / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "raccoon.project.yml").write_text(
        f"name: {project_name}\nuuid: {PROJECT_UUID}\n",
        encoding="utf-8",
    )
    return project_dir


def _write_run(project_dir: Path, run_id: str, frames: list[dict] | None = None,
               header: dict | None = None, include_localization: bool = True) -> Path:
    run_dir = project_dir / ".raccoon/runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if include_localization:
        path = run_dir / "localization.jsonl"
        lines: list[str] = []
        if header is None:
            header = {"kind": "header", "format_version": 1, "started_at_unix_ns": 0}
        lines.append(json.dumps(header))
        for f in frames or []:
            lines.append(json.dumps(f))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


def test_list_runs_empty_project(tmp_path: Path):
    _make_project(tmp_path)
    repo = RunRepository(tmp_path)
    assert repo.list_runs(PROJECT_UUID) == []


def test_list_runs_no_runs_dir(tmp_path: Path):
    _make_project(tmp_path)
    repo = RunRepository(tmp_path)
    # No .raccoon/runs/ created at all
    assert repo.list_runs(PROJECT_UUID) == []


def test_list_runs_single_run(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260523T143012Z", frames=[{"kind": "frame", "t_ns": 1000}])
    repo = RunRepository(tmp_path)
    runs = repo.list_runs(PROJECT_UUID)
    assert len(runs) == 1
    assert runs[0].run_id == "20260523T143012Z"
    assert runs[0].has_localization is True
    assert runs[0].file_size_bytes > 0


def test_list_runs_sorted_newest_first(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260101T000000Z")
    _write_run(project_dir, "20260523T143012Z")
    _write_run(project_dir, "20260301T120000Z")
    repo = RunRepository(tmp_path)
    runs = repo.list_runs(PROJECT_UUID)
    assert [r.run_id for r in runs] == [
        "20260523T143012Z",
        "20260301T120000Z",
        "20260101T000000Z",
    ]


def test_list_runs_skips_invalid_directory_names(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260523T143012Z")
    # Bogus directories
    (project_dir / ".raccoon/runs" / "junk").mkdir()
    (project_dir / ".raccoon/runs" / "2026-bad-format").mkdir()
    (project_dir / ".raccoon/runs" / "..").mkdir(exist_ok=True)  # treated as path, won't actually create a "..": skip
    repo = RunRepository(tmp_path)
    runs = repo.list_runs(PROJECT_UUID)
    assert [r.run_id for r in runs] == ["20260523T143012Z"]


def test_list_runs_skips_runs_without_localization_file(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260523T143012Z", include_localization=False)
    _write_run(project_dir, "20260524T143012Z")
    repo = RunRepository(tmp_path)
    runs = repo.list_runs(PROJECT_UUID)
    assert [r.run_id for r in runs] == ["20260524T143012Z"]


def test_list_runs_unknown_project(tmp_path: Path):
    repo = RunRepository(tmp_path)
    with pytest.raises(FileNotFoundError):
        repo.list_runs("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# get_localization_path / path traversal
# ---------------------------------------------------------------------------


def test_get_localization_path_returns_existing_file(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260523T143012Z")
    repo = RunRepository(tmp_path)
    path = repo.get_localization_path(PROJECT_UUID, "20260523T143012Z")
    assert path.is_file()
    assert path.name == "localization.jsonl"


@pytest.mark.parametrize(
    "bad",
    [
        "../../../etc/passwd",
        "../20260523T143012Z",
        "20260523T143012",
        "20260523t143012Z",
        "20260523T143012Z/../foo",
        "",
        "..",
    ],
)
def test_get_localization_path_rejects_traversal_attempts(tmp_path: Path, bad: str):
    _make_project(tmp_path)
    repo = RunRepository(tmp_path)
    with pytest.raises(ValueError):
        repo.get_localization_path(PROJECT_UUID, bad)


def test_get_localization_path_missing_file(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260523T143012Z", include_localization=False)
    repo = RunRepository(tmp_path)
    with pytest.raises(FileNotFoundError) as exc_info:
        repo.get_localization_path(PROJECT_UUID, "20260523T143012Z")
    assert "recording_missing" in str(exc_info.value)


# ---------------------------------------------------------------------------
# delete_run
# ---------------------------------------------------------------------------


def test_delete_run_removes_only_that_directory(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_run(project_dir, "20260523T143012Z")
    _write_run(project_dir, "20260524T143012Z")
    repo = RunRepository(tmp_path)

    repo.delete_run(PROJECT_UUID, "20260523T143012Z")

    runs_root = project_dir / ".raccoon/runs"
    assert not (runs_root / "20260523T143012Z").exists()
    assert (runs_root / "20260524T143012Z").exists()


def test_delete_run_rejects_traversal(tmp_path: Path):
    _make_project(tmp_path)
    repo = RunRepository(tmp_path)
    with pytest.raises(ValueError):
        repo.delete_run(PROJECT_UUID, "../../../etc")


def test_delete_run_missing(tmp_path: Path):
    _make_project(tmp_path)
    repo = RunRepository(tmp_path)
    with pytest.raises(FileNotFoundError):
        repo.delete_run(PROJECT_UUID, "20260101T000000Z")


# ---------------------------------------------------------------------------
# get_run_metadata
# ---------------------------------------------------------------------------


def test_get_run_metadata_computes_frame_count_and_duration(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    frames = [
        {"kind": "frame", "t_ns": 1_000_000},          # 1 ms
        {"kind": "frame", "t_ns": 250_000_000},        # 250 ms
        {"kind": "frame", "t_ns": 1_750_500_000},      # 1750 ms
    ]
    _write_run(project_dir, "20260523T143012Z", frames=frames)
    repo = RunRepository(tmp_path)

    meta = repo.get_run_metadata(PROJECT_UUID, "20260523T143012Z")

    assert meta.run_id == "20260523T143012Z"
    assert meta.has_localization is True
    assert meta.frame_count == 3
    assert meta.duration_ms == 1750
    assert meta.file_size_bytes > 0


def test_get_run_metadata_uses_real_sample_file(tmp_path: Path):
    """Smoke-test against the real sample JSONL if available."""
    sample = Path("/tmp/raccoon_sample.jsonl")
    if not sample.exists():
        pytest.skip("sample file not available")
    project_dir = _make_project(tmp_path)
    run_dir = project_dir / ".raccoon/runs" / "20260523T143012Z"
    run_dir.mkdir(parents=True)
    shutil.copy(sample, run_dir / "localization.jsonl")

    repo = RunRepository(tmp_path)
    meta = repo.get_run_metadata(PROJECT_UUID, "20260523T143012Z")
    # Sample has a header + 15 frames; final t_ns is 605061979 -> ~605 ms.
    assert meta.frame_count == 15
    assert meta.duration_ms == 605


def test_get_run_metadata_missing_run(tmp_path: Path):
    _make_project(tmp_path)
    repo = RunRepository(tmp_path)
    with pytest.raises(FileNotFoundError):
        repo.get_run_metadata(PROJECT_UUID, "20260523T143012Z")


# ---------------------------------------------------------------------------
# _read_last_line
# ---------------------------------------------------------------------------


def test_read_last_line_handles_trailing_newline(tmp_path: Path):
    f = tmp_path / "x.jsonl"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    assert _read_last_line(f) == b"c"


def test_read_last_line_no_trailing_newline(tmp_path: Path):
    f = tmp_path / "x.jsonl"
    f.write_text("a\nb\nlast", encoding="utf-8")
    assert _read_last_line(f) == b"last"


def test_read_last_line_single_line(tmp_path: Path):
    f = tmp_path / "x.jsonl"
    f.write_text("only", encoding="utf-8")
    assert _read_last_line(f) == b"only"


def test_read_last_line_empty(tmp_path: Path):
    f = tmp_path / "x.jsonl"
    f.write_text("", encoding="utf-8")
    assert _read_last_line(f) is None


def test_read_last_line_long_line_across_blocks(tmp_path: Path):
    f = tmp_path / "x.jsonl"
    big = "x" * 10_000
    f.write_text(f"first\n{big}\n", encoding="utf-8")
    assert _read_last_line(f) == big.encode("utf-8")
