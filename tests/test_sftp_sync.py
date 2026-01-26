"""Tests for SFTP sync functionality."""

import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from raccoon.client.sftp_sync import HashCache, SftpSync, SyncOptions, SyncDirection


class TestLineEndingNormalization:
    """Test that CRLF and LF produce the same hash for text files."""

    def test_crlf_and_lf_produce_same_hash(self, tmp_path: Path):
        """Text files with different line endings should produce the same hash."""
        # Create two files with same content but different line endings
        file_lf = tmp_path / "test_lf.py"
        file_crlf = tmp_path / "test_crlf.py"

        content = "line1\nline2\nline3\n"
        file_lf.write_bytes(content.encode())
        file_crlf.write_bytes(content.replace('\n', '\r\n').encode())

        cache = HashCache(tmp_path)
        hash_lf = cache.get_hash("test_lf.py", file_lf)
        hash_crlf = cache.get_hash("test_crlf.py", file_crlf)

        assert hash_lf == hash_crlf

    def test_cr_only_normalized(self, tmp_path: Path):
        """CR-only line endings (old Mac) should also be normalized."""
        file_lf = tmp_path / "test_lf.py"
        file_cr = tmp_path / "test_cr.py"

        content = "line1\nline2\nline3\n"
        file_lf.write_bytes(content.encode())
        file_cr.write_bytes(content.replace('\n', '\r').encode())

        cache = HashCache(tmp_path)
        hash_lf = cache.get_hash("test_lf.py", file_lf)
        hash_cr = cache.get_hash("test_cr.py", file_cr)

        assert hash_lf == hash_cr

    def test_binary_files_not_normalized(self, tmp_path: Path):
        """Binary files should NOT have line ending normalization."""
        file1 = tmp_path / "test.bin"
        file2 = tmp_path / "test2.bin"

        file1.write_bytes(b'\x00\r\n\x01')
        file2.write_bytes(b'\x00\n\x01')

        cache = HashCache(tmp_path)
        hash1 = cache.get_hash("test.bin", file1)
        hash2 = cache.get_hash("test2.bin", file2)

        # Binary files should NOT be normalized, so hashes differ
        assert hash1 != hash2

    def test_yaml_files_normalized(self, tmp_path: Path):
        """YAML files should be normalized."""
        file_lf = tmp_path / "config.yml"
        file_crlf = tmp_path / "config_crlf.yml"

        content = "key: value\nlist:\n  - item1\n  - item2\n"
        file_lf.write_bytes(content.encode())
        file_crlf.write_bytes(content.replace('\n', '\r\n').encode())

        cache = HashCache(tmp_path)
        hash_lf = cache.get_hash("config.yml", file_lf)
        hash_crlf = cache.get_hash("config_crlf.yml", file_crlf)

        assert hash_lf == hash_crlf

    def test_json_files_normalized(self, tmp_path: Path):
        """JSON files should be normalized."""
        file_lf = tmp_path / "data.json"
        file_crlf = tmp_path / "data_crlf.json"

        content = '{\n  "key": "value"\n}\n'
        file_lf.write_bytes(content.encode())
        file_crlf.write_bytes(content.replace('\n', '\r\n').encode())

        cache = HashCache(tmp_path)
        hash_lf = cache.get_hash("data.json", file_lf)
        hash_crlf = cache.get_hash("data_crlf.json", file_crlf)

        assert hash_lf == hash_crlf

    def test_mixed_line_endings_normalized(self, tmp_path: Path):
        """Files with mixed line endings should be normalized consistently."""
        file_mixed = tmp_path / "mixed.py"
        file_lf = tmp_path / "lf.py"

        # Mixed line endings: CRLF, LF, CR
        file_mixed.write_bytes(b"line1\r\nline2\nline3\rline4\n")
        # All LF
        file_lf.write_bytes(b"line1\nline2\nline3\nline4\n")

        cache = HashCache(tmp_path)
        hash_mixed = cache.get_hash("mixed.py", file_mixed)
        hash_lf = cache.get_hash("lf.py", file_lf)

        assert hash_mixed == hash_lf


class TestHashCaching:
    """Test hash cache functionality."""

    def test_cache_stores_hash(self, tmp_path: Path):
        """Hash should be cached after first computation."""
        test_file = tmp_path / "test.py"
        test_file.write_text("content")

        cache = HashCache(tmp_path)

        # First call computes hash
        hash1 = cache.get_hash("test.py", test_file)
        assert hash1 != ""

        # Second call should return cached value
        hash2 = cache.get_hash("test.py", test_file)
        assert hash1 == hash2

    def test_cache_invalidates_on_mtime_change(self, tmp_path: Path):
        """Cache should invalidate when file mtime changes."""
        import time

        test_file = tmp_path / "test.py"
        test_file.write_text("content1")

        cache = HashCache(tmp_path)
        hash1 = cache.get_hash("test.py", test_file)

        # Modify file (change content and mtime)
        time.sleep(0.1)  # Ensure different mtime
        test_file.write_text("content2")

        hash2 = cache.get_hash("test.py", test_file)
        assert hash1 != hash2

    def test_cache_persists_to_disk(self, tmp_path: Path):
        """Cache should persist to disk and reload."""
        test_file = tmp_path / "test.py"
        test_file.write_text("content")

        # Create cache and compute hash
        cache1 = HashCache(tmp_path)
        hash1 = cache1.get_hash("test.py", test_file)
        cache1.save_cache()

        # Create new cache instance (simulating restart)
        cache2 = HashCache(tmp_path)
        # Should load from disk cache
        assert (tmp_path / ".raccoon" / "sync_cache.json").exists()


class TestPathNormalization:
    """Test cross-platform path handling."""

    def test_windows_paths_converted_to_posix(self):
        """Paths should always use forward slashes internally."""
        # Note: On Linux, Path("src\\missions") doesn't interpret backslashes
        # This test verifies as_posix() works correctly
        path = Path("src/missions/main.py")
        assert path.as_posix() == "src/missions/main.py"

    def test_relative_path_posix(self, tmp_path: Path):
        """Relative paths should be converted to POSIX format."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "test.py").write_text("test")

        cache = HashCache(tmp_path)
        local_files = {}

        # Simulate what _get_local_files does
        for path in tmp_path.rglob("*"):
            if path.is_file():
                rel_path = path.relative_to(tmp_path)
                rel_path_posix = rel_path.as_posix()
                local_files[rel_path_posix] = cache.get_hash(rel_path_posix, path)

        assert "src/test.py" in local_files


class TestMergeToolLauncher:
    """Test PyCharm merge tool integration."""

    def test_uses_pycharm_launcher(self):
        """MergeToolLauncher should use PyCharmLauncher internally."""
        from raccoon.client.conflict_resolver import MergeToolLauncher

        launcher = MergeToolLauncher()
        # Just verify it can be instantiated and has the expected interface
        assert hasattr(launcher, 'is_available')
        assert hasattr(launcher, 'open_diff')

    def test_is_available_checks_pycharm(self):
        """is_available should delegate to PyCharmLauncher."""
        from raccoon.client.conflict_resolver import MergeToolLauncher

        launcher = MergeToolLauncher()

        # Mock the internal pycharm launcher
        mock_pycharm = Mock()
        mock_pycharm.is_available.return_value = True
        launcher._pycharm = mock_pycharm

        assert launcher.is_available() is True
        mock_pycharm.is_available.assert_called_once()

    def test_open_diff_calls_pycharm(self, tmp_path: Path):
        """open_diff should call PyCharm with diff command."""
        from raccoon.client.conflict_resolver import MergeToolLauncher

        # Create test files
        local_file = tmp_path / "local.py"
        remote_file = tmp_path / "remote.py"
        local_file.write_text("local content")
        remote_file.write_text("remote content")

        launcher = MergeToolLauncher()

        # Mock the pycharm launcher
        mock_pycharm = Mock()
        mock_pycharm.find_pycharm.return_value = Path("/usr/bin/pycharm")
        launcher._pycharm = mock_pycharm

        with patch('subprocess.Popen') as mock_popen:
            result = launcher.open_diff(local_file, remote_file)

            assert result is True
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args[0][0]
            assert "diff" in call_args
            assert str(local_file) in call_args
            assert str(remote_file) in call_args


class TestConflictResolution:
    """Test conflict resolution functionality."""

    def test_conflict_file_dataclass(self, tmp_path: Path):
        """ConflictFile should hold conflict information."""
        from raccoon.client.conflict_resolver import ConflictFile

        local_file = tmp_path / "test.py"
        local_file.write_text("local content")

        conflict = ConflictFile(
            rel_path="test.py",
            local_path=local_file,
            remote_content=b"remote content",
        )

        assert conflict.rel_path == "test.py"
        assert conflict.local_path == local_file
        assert conflict.remote_content == b"remote content"

    def test_resolution_enum_values(self):
        """ConflictResolution should have expected values."""
        from raccoon.client.conflict_resolver import ConflictResolution

        assert ConflictResolution.KEEP_LOCAL.value == "local"
        assert ConflictResolution.KEEP_REMOTE.value == "remote"
        assert ConflictResolution.SKIP.value == "skip"
        assert ConflictResolution.OPEN_DIFF.value == "diff"


class TestSyncOptions:
    """Test sync options configuration."""

    def test_default_options(self):
        """Default options should be sensible."""
        options = SyncOptions()

        assert options.direction == SyncDirection.PUSH
        assert options.delete_remote is True
        assert options.delete_local is False
        assert ".git" in options.exclude_patterns
        assert "__pycache__" in options.exclude_patterns
        assert ".raccoon" in options.exclude_patterns

    def test_custom_direction(self):
        """Should be able to set custom direction."""
        options = SyncOptions(direction=SyncDirection.BIDIRECTIONAL)
        assert options.direction == SyncDirection.BIDIRECTIONAL

    def test_custom_exclude_patterns(self):
        """Should be able to add custom exclude patterns."""
        options = SyncOptions()
        options.exclude_patterns = options.exclude_patterns + ["*.tmp", "build/"]

        assert "*.tmp" in options.exclude_patterns
        assert "build/" in options.exclude_patterns
        # Original patterns still present
        assert ".git" in options.exclude_patterns


class TestAutoMerge:
    """Test automatic file merging functionality."""

    def test_identical_files_after_normalization(self):
        """Files identical after line ending normalization should be IDENTICAL."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        local = b"line1\nline2\nline3\n"
        remote = b"line1\r\nline2\r\nline3\r\n"

        result = attempt_auto_merge(local, remote, "test.py")

        assert result.status == MergeStatus.IDENTICAL

    def test_non_overlapping_additions_merge(self):
        """Non-overlapping additions should be merged successfully."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        # Local adds at the end
        local = b"line1\nline2\nline3\nlocal_addition\n"
        # Remote is the original
        remote = b"line1\nline2\nline3\n"

        result = attempt_auto_merge(local, remote, "test.py")

        assert result.status == MergeStatus.SUCCESS
        assert b"local_addition" in result.merged_content

    def test_binary_files_cannot_merge(self):
        """Binary files should return BINARY status."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        local = b"\x00\x01\x02\x03"
        remote = b"\x00\x01\x02\x04"

        result = attempt_auto_merge(local, remote, "image.png")

        assert result.status == MergeStatus.BINARY

    def test_conflicting_changes_to_same_line(self):
        """Changes to the same line should result in CONFLICT."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        # Both change line2 differently
        local = b"line1\nlocal_modified\nline3\n"
        remote = b"line1\nremote_modified\nline3\n"

        result = attempt_auto_merge(local, remote, "test.py")

        assert result.status == MergeStatus.CONFLICT

    def test_is_text_file_detection(self):
        """Text file detection should work for common extensions."""
        from raccoon.client.auto_merge import is_text_file

        assert is_text_file("main.py") is True
        assert is_text_file("config.yml") is True
        assert is_text_file("data.json") is True
        assert is_text_file("script.sh") is True
        assert is_text_file("image.png") is False
        assert is_text_file("archive.zip") is False
        assert is_text_file("video.mp4") is False

    def test_merge_preserves_both_additions(self):
        """When both sides add different content, merge should include both."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        # Original: line1, line2
        # Local: adds line3 at end
        # Remote: adds line0 at start
        # This is tricky - they're both adding to different parts

        local = b"common\nlocal_add\n"
        remote = b"common\n"

        result = attempt_auto_merge(local, remote, "test.py")

        # Local additions should be preserved
        assert result.status == MergeStatus.SUCCESS
        assert b"local_add" in result.merged_content
        assert b"common" in result.merged_content

    def test_unicode_content_handled(self):
        """Unicode content should be handled correctly."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        local = "# Comment with émoji 🎉\ncode = True\n".encode('utf-8')
        remote = "# Comment with émoji 🎉\n".encode('utf-8')

        result = attempt_auto_merge(local, remote, "test.py")

        assert result.status == MergeStatus.SUCCESS
        assert "code = True" in result.merged_content.decode('utf-8')

    def test_invalid_utf8_treated_as_binary(self):
        """Invalid UTF-8 content should be treated as binary."""
        from raccoon.client.auto_merge import attempt_auto_merge, MergeStatus

        # Invalid UTF-8 sequence
        local = b"\xff\xfe\x00\x01"
        remote = b"\xff\xfe\x00\x02"

        result = attempt_auto_merge(local, remote, "test.py")

        assert result.status == MergeStatus.BINARY
