"""WebSocket client for streaming command output from Pi."""

import asyncio
import json
import time
from typing import Callable, Optional

from websocket import (
    WebSocket,
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


class OutputHandler:
    """
    Handles real-time output streaming from the Pi via WebSocket.

    Features:
    - Line-by-line output display
    - Rich formatting support
    - Cancellation handling
    """

    def __init__(
        self,
        websocket_url: str,
        recv_timeout: float = 0.5,
        cancel_grace: float = 5.0,
    ):
        """
        Initialize the output handler.

        Args:
            websocket_url: WebSocket URL for the command output stream
            recv_timeout: Timeout in seconds for each recv() call
            cancel_grace: After a cancel request, how long (seconds) to keep
                reading what the Pi still sends. The program writes its stderr
                (a traceback, a message from its cleanup) while it shuts down;
                without the grace period that tail would be dropped.
        """
        self.url = websocket_url
        self.ws: Optional[WebSocket] = None
        self._cancelled = False
        self._recv_timeout = recv_timeout
        self._cancel_grace = cancel_grace
        self._cancel_deadline: Optional[float] = None

    def stream_to_console(
        self,
        console: Optional[Console] = None,
        on_line: Optional[Callable[[str], None]] = None,
        echo: bool = True,
    ) -> dict:
        """
        Stream output to console until command completes.

        Args:
            console: Rich console for output (uses default if None)
            on_line: Optional callback for each line
            echo: When True, print each output line directly. Set False when the
                caller renders the lines itself (e.g. into a live TUI) via
                ``on_line`` and must not have them printed underneath it.

        Returns:
            Final status dict with exit_code
        """
        console = console or Console()

        try:
            self.ws = create_connection(self.url)
            self.ws.settimeout(self._recv_timeout)

            while True:
                # Cancellation was requested: keep draining until the Pi's final
                # status arrives (the program's last output comes before it), the
                # connection closes or the grace period is over.
                if self._cancelled:
                    if self._cancel_deadline is None:
                        self._cancel_deadline = time.monotonic() + self._cancel_grace
                    if time.monotonic() >= self._cancel_deadline:
                        return {"status": "cancelled", "exit_code": -1}

                try:
                    message = self.ws.recv()
                except WebSocketTimeoutException:
                    # Normal timeout — loop back to check cancel flag
                    continue
                except (WebSocketConnectionClosedException, OSError, EOFError):
                    if self._cancelled:
                        # The Pi closed the stream after the cancel: nothing lost.
                        return {"status": "cancelled", "exit_code": -1}
                    # Connection lost (robot died, network down, etc.)
                    console.print("[red]Connection to Pi lost.[/red]")
                    return {"status": "failed", "exit_code": -1, "error": "connection lost"}

                if not message:
                    continue

                # Control messages from the server are tagged with a unique
                # marker so we don't confuse program output that happens to
                # look like JSON with protocol messages.
                try:
                    data = json.loads(message)
                    if isinstance(data, dict) and data.get("__raccoon") == "control":
                        kind = data.get("kind")
                        if kind == "error":
                            console.print(f"[red]Error: {data.get('error', '')}[/red]")
                            return {"status": "failed", "error": data.get("error", "")}
                        if kind == "status":
                            if self._cancelled and data.get("exit_code") is None:
                                data["exit_code"] = -1
                            return data
                        # Unknown control kinds (e.g. "cancelling") are ignored.
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

                # Regular output line
                if echo:
                    print(message)
                if on_line:
                    on_line(message)

        except Exception as e:
            console.print(f"[red]Connection error: {e}[/red]")
            return {"status": "failed", "error": str(e)}

        finally:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None

    async def stream_to_console_async(
        self,
        console: Optional[Console] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Async version of stream_to_console."""
        # Run synchronous WebSocket in thread pool
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.stream_to_console(console, on_line)
        )

    def cancel(self) -> None:
        """Send cancel request to the Pi."""
        if self.ws:
            try:
                self.ws.send(json.dumps({"action": "cancel"}))
                self._cancelled = True
            except Exception:
                pass


def stream_command_output(
    address: str,
    port: int,
    command_id: str,
    console: Optional[Console] = None,
    api_token: Optional[str] = None,
) -> dict:
    """
    Convenience function to stream command output.

    Args:
        address: Pi address
        port: Pi port
        command_id: Command ID to stream
        console: Console for output
        api_token: API token for authentication

    Returns:
        Final status dict
    """
    url = f"ws://{address}:{port}/ws/output/{command_id}"
    if api_token:
        url += f"?token={api_token}"
    handler = OutputHandler(url)
    return handler.stream_to_console(console)
