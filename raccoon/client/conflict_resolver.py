"""Conflict resolution for SFTP sync."""

import difflib
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel


@dataclass
class ConflictFile:
    """Represents a file with a sync conflict."""

    rel_path: str
    local_path: Path
    remote_content: bytes  # Downloaded temp content


class ConflictResolution(Enum):
    """Resolution choice for a conflict."""

    KEEP_LOCAL = "local"
    KEEP_REMOTE = "remote"
    SKIP = "skip"
    OPEN_DIFF = "diff"


class MergeToolLauncher:
    """Launch PyCharm diff for conflict resolution."""

    def __init__(self):
        from raccoon.ide.launcher import PyCharmLauncher

        self._pycharm = PyCharmLauncher()

    def is_available(self) -> bool:
        """Check if PyCharm is available for diff viewing."""
        return self._pycharm.is_available()

    def open_diff(self, local_path: Path, remote_path: Path) -> bool:
        """
        Open PyCharm diff view for two files.

        Args:
            local_path: Path to the local version
            remote_path: Path to the remote version (temp file)

        Returns:
            True if PyCharm was launched successfully
        """
        pycharm_path = self._pycharm.find_pycharm()
        if not pycharm_path:
            return False

        try:
            # PyCharm CLI: pycharm diff <file1> <file2>
            subprocess.Popen(
                [str(pycharm_path), "diff", str(local_path), str(remote_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except Exception:
            return False


class ConflictResolver:
    """Interactive conflict resolution using Rich prompts."""

    def __init__(self):
        self._merge_tool = MergeToolLauncher()

    def resolve_conflicts(
        self,
        conflicts: list[ConflictFile],
        console: Console,
    ) -> dict[str, ConflictResolution]:
        """
        Interactively resolve each conflict.

        Args:
            conflicts: List of conflict files to resolve
            console: Rich console for output

        Returns:
            Dict mapping rel_path -> resolution
        """
        resolutions = {}

        for conflict in conflicts:
            resolution = self._prompt_for_resolution(conflict, console)
            resolutions[conflict.rel_path] = resolution

        return resolutions

    def _prompt_for_resolution(
        self, conflict: ConflictFile, console: Console
    ) -> ConflictResolution:
        """
        Prompt user for resolution of a single conflict.

        Args:
            conflict: The conflict to resolve
            console: Rich console for output

        Returns:
            The chosen resolution
        """
        console.print(f"\n[yellow]Conflict:[/yellow] {conflict.rel_path}")

        # Show inline diff
        self._show_inline_diff(conflict, console)

        # Build options
        options = ["l", "r", "s"]
        option_text = "[l]ocal, [r]emote, [s]kip"

        if self._merge_tool.is_available():
            options.append("p")
            option_text += ", [p]ycharm diff"

        while True:
            choice = click.prompt(
                f"Resolution ({option_text})",
                type=click.Choice(options, case_sensitive=False),
                default="s",
            )

            if choice == "l":
                return ConflictResolution.KEEP_LOCAL
            elif choice == "r":
                return ConflictResolution.KEEP_REMOTE
            elif choice == "s":
                return ConflictResolution.SKIP
            elif choice == "p":
                # Open in PyCharm and wait for user to finish merging
                self._open_in_pycharm(conflict, console)
                console.print()
                console.print("[cyan]PyCharm diff opened.[/cyan]")
                console.print("[dim]Edit the LOCAL file (left side) in PyCharm to merge changes.[/dim]")
                console.print("[dim]Save the file when done, then come back here.[/dim]")
                console.print()
                click.pause("Press Enter when done merging...")

                # After merging, ask if they want to use the (now edited) local version
                if click.confirm("Use your merged local version?", default=True):
                    return ConflictResolution.KEEP_LOCAL
                # Otherwise continue the loop to choose again

    def _show_inline_diff(self, conflict: ConflictFile, console: Console) -> None:
        """
        Show abbreviated inline diff between local and remote versions.

        Args:
            conflict: The conflict to display
            console: Rich console for output
        """
        try:
            # Read local content
            local_content = conflict.local_path.read_text(errors="replace")
            remote_content = conflict.remote_content.decode(errors="replace")

            local_lines = local_content.splitlines(keepends=True)
            remote_lines = remote_content.splitlines(keepends=True)

            # Generate unified diff
            diff = list(
                difflib.unified_diff(
                    remote_lines,
                    local_lines,
                    fromfile="remote",
                    tofile="local",
                    lineterm="",
                )
            )

            if not diff:
                console.print("[dim]  Files appear identical (possibly line ending differences)[/dim]")
                return

            # Show abbreviated diff (max 20 lines)
            max_lines = 20
            diff_text = "".join(diff[:max_lines])
            if len(diff) > max_lines:
                diff_text += f"\n... ({len(diff) - max_lines} more lines)"

            console.print(
                Panel(
                    Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
                    title="[cyan]Diff (remote -> local)[/cyan]",
                    border_style="dim",
                )
            )
        except Exception as e:
            console.print(f"[dim]  Could not display diff: {e}[/dim]")

    def _open_in_pycharm(self, conflict: ConflictFile, console: Console) -> None:
        """
        Open conflict in PyCharm diff viewer.

        Args:
            conflict: The conflict to open
            console: Rich console for output
        """
        # Write remote content to temp file
        suffix = conflict.local_path.suffix
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=f"_remote{suffix}", delete=False
        ) as f:
            f.write(conflict.remote_content)
            remote_temp = Path(f.name)

        try:
            if self._merge_tool.open_diff(conflict.local_path, remote_temp):
                console.print(f"[green]Opened diff in PyCharm[/green]")
            else:
                console.print("[red]Failed to open PyCharm diff[/red]")
        except Exception as e:
            console.print(f"[red]Error opening diff: {e}[/red]")


def prepare_conflict_files(
    conflicts: list[str],
    local_path: Path,
    remote_path: str,
    sftp,
) -> list[ConflictFile]:
    """
    Download remote versions of conflicting files to prepare for resolution.

    Args:
        conflicts: List of relative paths with conflicts
        local_path: Local project root
        remote_path: Remote project root
        sftp: SFTP client

    Returns:
        List of ConflictFile objects with remote content downloaded
    """
    conflict_files = []

    for rel_path in conflicts:
        local_file = local_path / rel_path
        remote_file = f"{remote_path}/{rel_path}"

        try:
            # Download remote content to memory
            with sftp.open(remote_file, "rb") as f:
                remote_content = f.read()

            conflict_files.append(
                ConflictFile(
                    rel_path=rel_path,
                    local_path=local_file,
                    remote_content=remote_content,
                )
            )
        except Exception:
            # Skip files we can't download
            pass

    return conflict_files


def apply_resolution(
    resolution: ConflictResolution,
    conflict: ConflictFile,
    remote_path: str,
    sftp,
    hash_cache,
    remote_manifest,
) -> Optional[str]:
    """
    Apply a conflict resolution.

    Args:
        resolution: The chosen resolution
        conflict: The conflict file
        remote_path: Remote project root
        sftp: SFTP client
        hash_cache: Local hash cache
        remote_manifest: Remote manifest

    Returns:
        Error message if failed, None if successful
    """
    if resolution == ConflictResolution.SKIP:
        return None

    rel_path = conflict.rel_path
    local_file = conflict.local_path
    remote_file = f"{remote_path}/{rel_path}"

    try:
        if resolution == ConflictResolution.KEEP_LOCAL:
            # Upload local version to remote
            sftp.put(str(local_file), remote_file)

            # Update manifest with local hash
            file_hash = hash_cache.get_hash(rel_path, local_file)
            file_stat = local_file.stat()
            remote_manifest.set(rel_path, file_hash, file_stat.st_mtime, file_stat.st_size)

        elif resolution == ConflictResolution.KEEP_REMOTE:
            # Write remote content to local
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_bytes(conflict.remote_content)

            # Invalidate local cache and update manifest
            hash_cache.invalidate(rel_path)
            new_hash = hash_cache.get_hash(rel_path, local_file)
            file_stat = local_file.stat()
            remote_manifest.set(rel_path, new_hash, file_stat.st_mtime, file_stat.st_size)

        return None
    except Exception as e:
        return f"Failed to apply resolution for {rel_path}: {e}"
