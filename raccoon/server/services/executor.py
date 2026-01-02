"""Command execution service with async output streaming."""

import asyncio
import os
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncGenerator, Optional


class CommandStatus(str, Enum):
    """Command execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommandExecutor:
    """
    Executes commands asynchronously with real-time output streaming.

    Features:
    - Async subprocess execution
    - Line-by-line output streaming
    - Output buffering for late-connecting clients
    - Graceful cancellation
    """

    def __init__(self, buffer_size: int = 1000):
        """
        Initialize the executor.

        Args:
            buffer_size: Maximum number of output lines to buffer
        """
        self.buffer_size = buffer_size
        self.status = CommandStatus.PENDING
        self.exit_code: Optional[int] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self._output_buffer: list[str] = []
        self._process: Optional[asyncio.subprocess.Process] = None
        self._output_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._subscribers: list[asyncio.Queue] = []

    @property
    def output_line_count(self) -> int:
        """Number of output lines captured."""
        return len(self._output_buffer)

    async def execute(
        self,
        command_id: str,
        project_path: Path,
        command: str,
        args: list[str],
        env: Optional[dict[str, str]] = None,
    ) -> int:
        """
        Execute a command and stream output.

        Args:
            command_id: Unique identifier for this command execution
            project_path: Working directory for the command
            command: The command to execute
            args: Command arguments
            env: Additional environment variables

        Returns:
            Exit code of the command
        """
        self.status = CommandStatus.RUNNING
        self.started_at = datetime.utcnow().isoformat()

        # Prepare environment
        cmd_env = os.environ.copy()
        if env:
            cmd_env.update(env)

        try:
            # Create subprocess with pipes for stdout/stderr
            self._process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout
                cwd=str(project_path),
                env=cmd_env,
            )

            # Read output line by line
            async for line in self._read_output():
                self._buffer_line(line)
                await self._broadcast_line(line)

            # Wait for process to complete
            await self._process.wait()
            self.exit_code = self._process.returncode

            if self.exit_code == 0:
                self.status = CommandStatus.COMPLETED
            else:
                self.status = CommandStatus.FAILED

        except asyncio.CancelledError:
            self.status = CommandStatus.CANCELLED
            if self._process:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._process.kill()
            raise

        except Exception as e:
            self.status = CommandStatus.FAILED
            self._buffer_line(f"Error: {str(e)}")
            await self._broadcast_line(f"Error: {str(e)}")

        finally:
            self.finished_at = datetime.utcnow().isoformat()
            # Signal end of output to all subscribers
            await self._broadcast_line(None)

        return self.exit_code or -1

    async def _read_output(self) -> AsyncGenerator[str, None]:
        """Read output from the process line by line."""
        if not self._process or not self._process.stdout:
            return

        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip("\n\r")

    def _buffer_line(self, line: str) -> None:
        """Add a line to the output buffer."""
        self._output_buffer.append(line)
        # Trim buffer if too large
        if len(self._output_buffer) > self.buffer_size:
            self._output_buffer = self._output_buffer[-self.buffer_size :]

    async def _broadcast_line(self, line: Optional[str]) -> None:
        """Broadcast a line to all subscribers."""
        for queue in self._subscribers:
            await queue.put(line)

    def subscribe(self) -> asyncio.Queue:
        """
        Subscribe to output stream.

        Returns a queue that will receive output lines.
        None indicates end of output.
        """
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._subscribers.append(queue)

        # Send buffered output to new subscriber
        for line in self._output_buffer:
            queue.put_nowait(line)

        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from output stream."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def cancel(self) -> None:
        """Cancel the running command."""
        if self._process and self.status == CommandStatus.RUNNING:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
            self.status = CommandStatus.CANCELLED

    def get_buffered_output(self) -> list[str]:
        """Get all buffered output lines."""
        return list(self._output_buffer)
