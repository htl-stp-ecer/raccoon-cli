"""Tests for the content fingerprint module."""

import os
from pathlib import Path

import pytest

from raccoon.fingerprint import (
    FingerprintResult,
    compute_fingerprint,
    default_exclude_patterns,
)


def _write(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestComputeFingerprint:
    """compute_fingerprint walks a tree and produces a deterministic hash."""

    def test_empty_tree_is_stable(self, tmp_path: Path):
        """An empty directory has a well-defined, constant root hash."""
        a = compute_fingerprint(tmp_path)
        b = compute_fingerprint(tmp_path)
        assert a.root_hash == b.root_hash
        assert a.file_count == 0
        assert a.total_bytes == 0

    def test_root_hash_is_deterministic(self, tmp_path: Path):
        """Same files + same contents = same hash."""
        _write(tmp_path / "a.txt", b"hello")
        _write(tmp_path / "nested/b.txt", b"world")
        first = compute_fingerprint(tmp_path)
        second = compute_fingerprint(tmp_path)
        assert first.root_hash == second.root_hash
        assert first.file_count == 2
        assert first.total_bytes == len(b"hello") + len(b"world")

    def test_content_change_changes_root_hash(self, tmp_path: Path):
        """Flipping a single byte changes the root hash."""
        _write(tmp_path / "a.txt", b"hello")
        before = compute_fingerprint(tmp_path).root_hash

        _write(tmp_path / "a.txt", b"hellO")
        after = compute_fingerprint(tmp_path).root_hash

        assert before != after

    def test_rename_changes_root_hash(self, tmp_path: Path):
        """Renaming a file (same content) changes the root hash — paths are hashed."""
        _write(tmp_path / "a.txt", b"same")
        first = compute_fingerprint(tmp_path).root_hash

        (tmp_path / "a.txt").rename(tmp_path / "b.txt")
        second = compute_fingerprint(tmp_path).root_hash

        assert first != second

    def test_order_independent(self, tmp_path: Path, tmp_path_factory):
        """Two trees with identical content but created in different order match."""
        _write(tmp_path / "a.txt", b"A")
        _write(tmp_path / "b.txt", b"B")
        _write(tmp_path / "c/d.txt", b"D")

        other = tmp_path_factory.mktemp("other")
        _write(other / "c/d.txt", b"D")
        _write(other / "b.txt", b"B")
        _write(other / "a.txt", b"A")

        assert compute_fingerprint(tmp_path).root_hash == compute_fingerprint(other).root_hash

    def test_excludes_are_respected(self, tmp_path: Path):
        """Excluded files must not affect the root hash or file_count."""
        _write(tmp_path / "keep.txt", b"keep")
        _write(tmp_path / "ignore.log", b"junk")
        _write(tmp_path / "__pycache__/x.pyc", b"bytecode")

        result = compute_fingerprint(
            tmp_path, exclude_patterns=default_exclude_patterns()
        )
        assert "keep.txt" in result.files
        assert "ignore.log" not in result.files
        assert all("__pycache__" not in k for k in result.files)
        assert result.file_count == 1

    def test_generated_files_are_included(self, tmp_path: Path):
        """Per user spec: codegen outputs are NOT excluded from the fingerprint."""
        _write(tmp_path / "src/hardware/defs.py", b"# generated")
        _write(tmp_path / "src/hardware/robot.py", b"class Robot: pass")

        result = compute_fingerprint(tmp_path)
        assert "src/hardware/defs.py" in result.files
        assert "src/hardware/robot.py" in result.files

    def test_symlinks_are_skipped(self, tmp_path: Path):
        """Symlinks are platform-dependent and must not contribute."""
        _write(tmp_path / "real.txt", b"real")
        link = tmp_path / "link.txt"
        try:
            os.symlink(tmp_path / "real.txt", link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        result = compute_fingerprint(tmp_path)
        assert "real.txt" in result.files
        assert "link.txt" not in result.files

    def test_nonexistent_root_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            compute_fingerprint(tmp_path / "does-not-exist")


class TestDiff:
    """FingerprintResult.diff buckets the differences correctly."""

    def test_identical_trees_have_empty_diff(self, tmp_path: Path):
        _write(tmp_path / "a.txt", b"a")
        r1 = compute_fingerprint(tmp_path)
        r2 = compute_fingerprint(tmp_path)
        d = r1.diff(r2)
        assert d == {"only_in_self": [], "only_in_other": [], "changed": []}

    def test_detects_added_and_removed(self):
        r1 = FingerprintResult(
            root_hash="x", files={"a": "h1", "b": "h2"}
        )
        r2 = FingerprintResult(
            root_hash="y", files={"b": "h2", "c": "h3"}
        )
        d = r1.diff(r2)
        assert d["only_in_self"] == ["a"]
        assert d["only_in_other"] == ["c"]
        assert d["changed"] == []

    def test_detects_changed(self):
        r1 = FingerprintResult(root_hash="x", files={"a": "h1"})
        r2 = FingerprintResult(root_hash="y", files={"a": "h2"})
        d = r1.diff(r2)
        assert d["changed"] == ["a"]
        assert d["only_in_self"] == []
        assert d["only_in_other"] == []
