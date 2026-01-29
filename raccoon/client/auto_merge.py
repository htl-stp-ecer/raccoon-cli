"""Automatic text file merging for bidirectional sync."""

import difflib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class MergeStatus(Enum):
    """Result of a merge attempt."""

    SUCCESS = "success"  # Merged without conflicts
    CONFLICT = "conflict"  # Could not auto-merge, needs manual resolution
    BINARY = "binary"  # Binary file, cannot merge
    IDENTICAL = "identical"  # Files are identical after normalization


@dataclass
class MergeResult:
    """Result of attempting to merge two versions."""

    status: MergeStatus
    merged_content: Optional[bytes] = None
    conflict_markers: Optional[str] = None  # For showing what conflicted
    local_only_changes: int = 0  # Lines only changed locally
    remote_only_changes: int = 0  # Lines only changed remotely


# Text file extensions that can be auto-merged
TEXT_EXTENSIONS = {
    '.py', '.yml', '.yaml', '.json', '.txt', '.md', '.rst',
    '.cfg', '.ini', '.toml', '.sh', '.bash', '.zsh',
    '.html', '.css', '.js', '.ts', '.jsx', '.tsx',
    '.xml', '.csv', '.env', '.gitignore', '.dockerignore',
    '.c', '.h', '.cpp', '.hpp', '.java', '.go', '.rs',
}


def is_text_file(path: str) -> bool:
    """Check if a file is a text file based on extension."""
    from pathlib import Path
    suffix = Path(path).suffix.lower()
    return suffix in TEXT_EXTENSIONS


def normalize_content(content: bytes) -> str:
    """
    Normalize content for merging.

    Handles line endings and decodes to string.
    """
    try:
        # Normalize line endings
        content = content.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        return content.decode('utf-8')
    except UnicodeDecodeError:
        return None  # Binary file


def attempt_auto_merge(
    local_content: bytes,
    remote_content: bytes,
    rel_path: str,
) -> MergeResult:
    """
    Attempt to automatically merge local and remote versions.

    Uses a line-based approach:
    1. Find lines unique to local (local additions/changes)
    2. Find lines unique to remote (remote additions/changes)
    3. If changes don't overlap, merge them
    4. If changes overlap, report conflict

    Args:
        local_content: Content of local file
        remote_content: Content of remote file
        rel_path: Relative path (for extension checking)

    Returns:
        MergeResult with status and merged content if successful
    """
    # Check if text file
    if not is_text_file(rel_path):
        return MergeResult(status=MergeStatus.BINARY)

    # Normalize and decode
    local_text = normalize_content(local_content)
    remote_text = normalize_content(remote_content)

    if local_text is None or remote_text is None:
        return MergeResult(status=MergeStatus.BINARY)

    # Check if identical after normalization
    if local_text == remote_text:
        return MergeResult(
            status=MergeStatus.IDENTICAL,
            merged_content=local_content,
        )

    # Split into lines
    local_lines = local_text.splitlines(keepends=True)
    remote_lines = remote_text.splitlines(keepends=True)

    # Ensure files end with newline for clean merging
    if local_lines and not local_lines[-1].endswith('\n'):
        local_lines[-1] += '\n'
    if remote_lines and not remote_lines[-1].endswith('\n'):
        remote_lines[-1] += '\n'

    # Get sequence matcher to find matching blocks
    matcher = difflib.SequenceMatcher(None, local_lines, remote_lines)

    # Try to merge using the matching blocks
    merged, has_conflict, local_changes, remote_changes = _merge_via_matching_blocks(
        local_lines, remote_lines, matcher
    )

    if has_conflict:
        # Generate conflict markers for display
        conflict_diff = list(difflib.unified_diff(
            remote_lines, local_lines,
            fromfile='remote', tofile='local',
            lineterm=''
        ))
        conflict_text = ''.join(conflict_diff[:50])  # First 50 lines

        return MergeResult(
            status=MergeStatus.CONFLICT,
            conflict_markers=conflict_text,
            local_only_changes=local_changes,
            remote_only_changes=remote_changes,
        )

    merged_content = ''.join(merged).encode('utf-8')
    return MergeResult(
        status=MergeStatus.SUCCESS,
        merged_content=merged_content,
        local_only_changes=local_changes,
        remote_only_changes=remote_changes,
    )


def _merge_via_matching_blocks(
    local_lines: list[str],
    remote_lines: list[str],
    matcher: difflib.SequenceMatcher,
) -> tuple[list[str], bool, int, int]:
    """
    Merge two versions using matching blocks as anchors.

    Strategy:
    - Matching blocks are kept as-is
    - Between matching blocks, if only one side has changes, take those
    - If both sides have different changes in the same region, conflict

    Returns:
        (merged_lines, has_conflict, local_change_count, remote_change_count)
    """
    merged = []
    has_conflict = False
    local_changes = 0
    remote_changes = 0

    # Get matching blocks (sequences that appear in both)
    blocks = matcher.get_matching_blocks()

    prev_local_end = 0
    prev_remote_end = 0

    for block in blocks:
        local_start, remote_start, size = block

        # Get the non-matching regions before this block
        local_gap = local_lines[prev_local_end:local_start]
        remote_gap = remote_lines[prev_remote_end:remote_start]

        if local_gap or remote_gap:
            # We have differences in this region
            if not local_gap:
                # Only remote has additions
                merged.extend(remote_gap)
                remote_changes += len(remote_gap)
            elif not remote_gap:
                # Only local has additions
                merged.extend(local_gap)
                local_changes += len(local_gap)
            elif local_gap == remote_gap:
                # Same changes on both sides
                merged.extend(local_gap)
            else:
                # Different changes - try line-by-line merge
                line_merged, line_conflict = _try_line_merge(local_gap, remote_gap)
                if line_conflict:
                    has_conflict = True
                    # For now, prefer local on conflict (will be flagged)
                    merged.extend(local_gap)
                else:
                    merged.extend(line_merged)
                    local_changes += len([l for l in local_gap if l not in remote_gap])
                    remote_changes += len([l for l in remote_gap if l not in local_gap])

        # Add the matching block
        merged.extend(local_lines[local_start:local_start + size])

        prev_local_end = local_start + size
        prev_remote_end = remote_start + size

    return merged, has_conflict, local_changes, remote_changes


def _try_line_merge(local_gap: list[str], remote_gap: list[str]) -> tuple[list[str], bool]:
    """
    Try to merge two different gap regions.

    Uses a simple heuristic:
    - If one gap is a subset of the other, use the larger one
    - If gaps have common lines and unique additions, merge them
    - Otherwise, conflict

    Returns:
        (merged_lines, has_conflict)
    """
    local_set = set(local_gap)
    remote_set = set(remote_gap)

    # Check if one is subset of other
    if local_set.issubset(remote_set):
        return remote_gap, False
    if remote_set.issubset(local_set):
        return local_gap, False

    # Find common lines
    common = local_set & remote_set
    local_only = [l for l in local_gap if l not in remote_set]
    remote_only = [l for l in remote_gap if l not in local_set]

    # If no common lines and both have unique content, it's a true modification conflict
    # (both sides changed the same content differently, not just adding)
    if not common and local_only and remote_only:
        # Both modified the same region with no common ground
        return [], True

    # Try to merge: interleave based on ordering
    # This is a simplistic merge that may not preserve order perfectly
    # but it's better than conflicting on every difference

    # Try to preserve order by using longest common subsequence
    merged = []
    local_idx = 0
    remote_idx = 0

    while local_idx < len(local_gap) or remote_idx < len(remote_gap):
        if local_idx >= len(local_gap):
            merged.extend(remote_gap[remote_idx:])
            break
        if remote_idx >= len(remote_gap):
            merged.extend(local_gap[local_idx:])
            break

        local_line = local_gap[local_idx]
        remote_line = remote_gap[remote_idx]

        if local_line == remote_line:
            merged.append(local_line)
            local_idx += 1
            remote_idx += 1
        elif local_line in remote_set:
            # Local line exists in remote, but later - take remote line first
            merged.append(remote_line)
            remote_idx += 1
        elif remote_line in local_set:
            # Remote line exists in local, but later - take local line first
            merged.append(local_line)
            local_idx += 1
        else:
            # Both are unique - this is a conflict
            return [], True

    return merged, False


def can_auto_merge(local_content: bytes, remote_content: bytes, rel_path: str) -> bool:
    """
    Quick check if auto-merge is possible without computing full result.

    Returns True if the file is a text file and might be mergeable.
    """
    if not is_text_file(rel_path):
        return False

    # Check if content is valid text
    try:
        local_content.decode('utf-8')
        remote_content.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False
