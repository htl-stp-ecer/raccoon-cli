"""Tests for OutputHandler — connection loss, cancellation, normal flow."""

import json
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from raccoon.client.output_handler import OutputHandler


class FakeWebSocket:
    """Minimal WebSocket stub for testing OutputHandler."""

    def __init__(self, messages=None):
        """
        Args:
            messages: list of items to return from recv().
                - str: returned as-is
                - Exception instance: raised from recv()
        """
        self._messages = list(messages or [])
        self._idx = 0
        self.timeout = None
        self.sent: list[str] = []
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def recv(self):
        if self._idx >= len(self._messages):
            raise StopIteration("no more messages")
        msg = self._messages[self._idx]
        self._idx += 1
        if isinstance(msg, BaseException):
            raise msg
        return msg

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def _make_handler(messages, recv_timeout=0.01):
    """Create an OutputHandler with a fake WebSocket."""
    ws = FakeWebSocket(messages)
    handler = OutputHandler("ws://fake:1234/ws/output/test-id", recv_timeout=recv_timeout)

    with patch("raccoon.client.output_handler.create_connection", return_value=ws):
        yield handler, ws


@pytest.fixture()
def console():
    return Console(force_terminal=True, width=120)


def test_normal_output_then_completed(console):
    """Lines are printed, then final status JSON stops the loop."""
    status_msg = json.dumps({"status": "completed", "exit_code": 0})
    messages = ["line 1", "line 2", status_msg]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert ws.closed


def test_error_json_returns_failed(console):
    """An error JSON message returns failed status."""
    messages = [json.dumps({"error": "something broke"})]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "failed"
    assert "something broke" in result["error"]


def test_connection_closed_returns_failed(console):
    """WebSocketConnectionClosedException exits with connection lost."""
    from websocket import WebSocketConnectionClosedException

    messages = ["line 1", WebSocketConnectionClosedException()]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "failed"
    assert result["error"] == "connection lost"


def test_oserror_returns_connection_lost(console):
    """OSError (e.g. broken pipe) exits with connection lost."""
    messages = ["line 1", OSError("Connection reset by peer")]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "failed"
    assert result["error"] == "connection lost"


def test_eoferror_returns_connection_lost(console):
    """EOFError exits with connection lost."""
    messages = [EOFError()]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "failed"
    assert result["error"] == "connection lost"


def test_timeout_loops_until_data(console):
    """WebSocketTimeoutException is retried, then real data is received."""
    from websocket import WebSocketTimeoutException

    status_msg = json.dumps({"status": "completed", "exit_code": 0})
    messages = [
        WebSocketTimeoutException(),
        WebSocketTimeoutException(),
        "output line",
        status_msg,
    ]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "completed"


def test_cancel_flag_exits_loop(console):
    """Setting _cancelled causes the loop to exit on next iteration."""
    from websocket import WebSocketTimeoutException

    # Timeout gives the loop a chance to check _cancelled
    messages = [WebSocketTimeoutException()] * 10

    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler._cancelled = True
    result = handler.stream_to_console(console)

    assert result["status"] == "cancelled"


def test_cancel_sends_json_to_ws():
    """cancel() sends a JSON cancel action and sets the flag."""
    ws = FakeWebSocket()
    handler = OutputHandler("ws://fake:1234")
    handler.ws = ws

    handler.cancel()

    assert handler._cancelled
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {"action": "cancel"}


def test_on_line_callback(console):
    """on_line callback is invoked for each output line."""
    status_msg = json.dumps({"status": "completed", "exit_code": 0})
    messages = ["alpha", "beta", status_msg]

    lines: list[str] = []
    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler.stream_to_console(console, on_line=lines.append)

    assert lines == ["alpha", "beta"]
