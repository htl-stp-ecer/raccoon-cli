"""WebSocket client for streaming LCM messages from Pi."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Callable, Optional

from websocket import WebSocket, create_connection
from rich.console import Console

from raccoon_cli.server.services.lcm_spy import decode_lcm_message


class LcmOutputHandler:
    """
    Handles real-time LCM message streaming from the Pi via WebSocket.

    Supports multiple output formats:
    - table: Formatted columns with channel, timestamp, size
    - json: Raw JSON output
    - compact: Single-line per message
    """

    def __init__(self, websocket_url: str, format: str = "table"):
        """
        Initialize the LCM output handler.

        Args:
            websocket_url: WebSocket URL for the LCM stream
            format: Output format (table, json, compact)
        """
        self.url = websocket_url
        self.format = format
        self.ws: Optional[WebSocket] = None
        self._message_count = 0
        self._channels_seen: set[str] = set()

    def stream_to_console(
        self,
        console: Optional[Console] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        on_message: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """
        Stream LCM messages to console.

        Args:
            console: Rich console for output
            stop_check: Callable that returns True when streaming should stop
            on_message: Optional callback for each message

        Returns:
            Final status dict
        """
        console = console or Console()

        try:
            self.ws = create_connection(self.url)

            while True:
                # Check stop condition
                if stop_check and stop_check():
                    break

                # Non-blocking receive with timeout
                self.ws.settimeout(0.5)
                try:
                    message = self.ws.recv()
                except Exception:
                    continue

                if not message:
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                # Check for status/error messages
                msg_type = data.get("type", "")

                if msg_type == "status":
                    # Status update (stopped, etc.)
                    return data

                if msg_type == "error":
                    console.print(f"[red]Error: {data.get('error', 'Unknown error')}[/red]")
                    return data

                if msg_type == "message":
                    # Regular LCM message
                    self._message_count += 1
                    self._channels_seen.add(data.get("channel", ""))

                    # Client-side decoding fallback if server didn't decode
                    if not data.get("decoded") and data.get("data_base64"):
                        try:
                            raw = base64.b64decode(data["data_base64"])
                            decoded = decode_lcm_message(raw, data.get("channel", ""))
                            if decoded:
                                data["decoded"] = decoded
                        except Exception:
                            pass

                    self._display_message(console, data)
                    if on_message:
                        on_message(data)

            return {
                "type": "status",
                "status": "stopped",
                "message_count": self._message_count,
                "channels_seen": list(self._channels_seen),
            }

        except Exception as e:
            console.print(f"[red]Connection error: {e}[/red]")
            return {"type": "error", "status": "error", "error": str(e)}

        finally:
            if self.ws:
                self.ws.close()
                self.ws = None

    async def stream_to_console_async(
        self,
        console: Optional[Console] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        on_message: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Async version of stream_to_console."""
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.stream_to_console(console, stop_check, on_message)
        )

    def stop(self) -> None:
        """Send stop request to the Pi."""
        if self.ws:
            try:
                self.ws.send(json.dumps({"action": "stop"}))
            except Exception:
                pass

    def _display_message(self, console: Console, data: dict) -> None:
        """Display a message in the configured format."""
        decoded = data.get("decoded")

        if self.format == "json":
            # Raw JSON output
            console.print(json.dumps(data))

        elif self.format == "compact":
            # Single-line compact format
            channel = data.get("channel", "?")
            size = data.get("data_size", 0)
            ts = data.get("timestamp", "")[:23]  # Trim to ms precision

            if decoded:
                # Show decoded content
                type_name = decoded.get("_type", "?")
                # Format decoded values (excluding _type)
                values = {k: v for k, v in decoded.items() if k != "_type"}
                value_str = ", ".join(f"{k}={v}" for k, v in values.items())
                console.print(f"[cyan]{channel}[/cyan] [green]{type_name}[/green] {value_str}")
            else:
                console.print(f"[cyan]{channel}[/cyan] [{size}B] {ts}")

        else:
            # Table format (default)
            channel = data.get("channel", "?")
            size = data.get("data_size", 0)
            ts = data.get("timestamp", "")[:23]

            if decoded:
                # Show decoded content
                type_name = decoded.get("_type", "?")
                # Format decoded values (excluding _type)
                values = {k: v for k, v in decoded.items() if k != "_type"}
                value_str = ", ".join(f"{k}={v}" for k, v in values.items())
                if len(value_str) > 50:
                    value_str = value_str[:47] + "..."

                console.print(
                    f"[cyan]{channel:24}[/cyan] "
                    f"[dim]{ts}[/dim] "
                    f"[green]{type_name:16}[/green] "
                    f"[white]{value_str}[/white]"
                )
            else:
                # Fallback to base64 preview
                data_preview = data.get("data_base64", "")[:24]
                if len(data.get("data_base64", "")) > 24:
                    data_preview += "..."

                console.print(
                    f"[cyan]{channel:24}[/cyan] "
                    f"[dim]{ts}[/dim] "
                    f"[yellow]{size:6}B[/yellow] "
                    f"[dim]{data_preview}[/dim]"
                )

    @property
    def message_count(self) -> int:
        """Number of messages received."""
        return self._message_count

    @property
    def channels_seen(self) -> set[str]:
        """Set of channels seen."""
        return self._channels_seen.copy()
