"""WebSocket client for streaming command output from Pi."""

import asyncio
import json
from typing import Callable, Optional

from websocket import WebSocket, create_connection
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

    def __init__(self, websocket_url: str):
        """
        Initialize the output handler.

        Args:
            websocket_url: WebSocket URL for the command output stream
        """
        self.url = websocket_url
        self.ws: Optional[WebSocket] = None
        self._cancelled = False

    def stream_to_console(
        self,
        console: Optional[Console] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Stream output to console until command completes.

        Args:
            console: Rich console for output (uses default if None)
            on_line: Optional callback for each line

        Returns:
            Final status dict with exit_code
        """
        console = console or Console()

        try:
            self.ws = create_connection(self.url)

            while True:
                message = self.ws.recv()

                if not message:
                    continue

                # Check if it's a JSON status message
                try:
                    data = json.loads(message)
                    if "status" in data:
                        # Final status message
                        return data
                    if "error" in data:
                        console.print(f"[red]Error: {data['error']}[/red]")
                        return {"status": "failed", "error": data["error"]}
                except json.JSONDecodeError:
                    pass

                # Regular output line
                console.print(message)
                if on_line:
                    on_line(message)

        except Exception as e:
            console.print(f"[red]Connection error: {e}[/red]")
            return {"status": "failed", "error": str(e)}

        finally:
            if self.ws:
                self.ws.close()
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
) -> dict:
    """
    Convenience function to stream command output.

    Args:
        address: Pi address
        port: Pi port
        command_id: Command ID to stream
        console: Console for output

    Returns:
        Final status dict
    """
    url = f"ws://{address}:{port}/ws/output/{command_id}"
    handler = OutputHandler(url)
    return handler.stream_to_console(console)
