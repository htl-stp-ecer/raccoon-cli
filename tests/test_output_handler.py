"""Tests for OutputHandler — connection loss, cancellation, normal flow."""

import json
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from raccoon_cli.client.output_handler import OutputHandler


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


def _make_handler(messages, recv_timeout=0.01, cancel_grace=5.0):
    """Create an OutputHandler with a fake WebSocket."""
    ws = FakeWebSocket(messages)
    handler = OutputHandler(
        "ws://fake:1234/ws/output/test-id",
        recv_timeout=recv_timeout,
        cancel_grace=cancel_grace,
    )

    with patch("raccoon_cli.client.output_handler.create_connection", return_value=ws):
        yield handler, ws


@pytest.fixture()
def console():
    return Console(force_terminal=True, width=120)


def _status(exit_code=0, status="completed"):
    return json.dumps(
        {
            "__raccoon": "control",
            "kind": "status",
            "status": status,
            "exit_code": exit_code,
        }
    )


def test_normal_output_then_completed(console):
    """Lines are printed, then final status JSON stops the loop."""
    messages = ["line 1", "line 2", _status()]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert ws.closed


def test_error_control_returns_failed(console):
    """A tagged error control message returns failed status."""
    messages = [
        json.dumps(
            {"__raccoon": "control", "kind": "error", "error": "something broke"}
        )
    ]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "failed"
    assert "something broke" in result["error"]


def test_untagged_json_with_error_is_printed_as_output(console):
    """Program output that looks like JSON with 'error' is not a protocol msg."""
    # A run that prints JSON-like log lines should NOT be interpreted as a
    # protocol error. The stream must continue until the real status arrives.
    lines: list[str] = []
    messages = [
        json.dumps({"level": "info", "error": None, "msg": "heartbeat"}),
        "plain line",
        _status(exit_code=0),
    ]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console, on_line=lines.append)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    # Both the JSON-looking line and the plain line were delivered as output.
    assert len(lines) == 2
    assert "plain line" in lines


def test_untagged_json_with_status_is_printed_as_output(console):
    """Output lines that happen to contain a 'status' key are not protocol msgs."""
    messages = [
        json.dumps({"status": "running", "exit_code": 3}),
        _status(exit_code=0),
    ]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0


def test_cancelling_control_is_ignored(console):
    """The server's cancelling ack doesn't end the stream."""
    messages = [
        json.dumps({"__raccoon": "control", "kind": "cancelling"}),
        "still streaming",
        _status(exit_code=0),
    ]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    result = handler.stream_to_console(console)

    assert result["status"] == "completed"


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

    messages = [
        WebSocketTimeoutException(),
        WebSocketTimeoutException(),
        "output line",
        _status(exit_code=0),
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

    gen = _make_handler(messages, cancel_grace=0)
    handler, ws = next(gen)
    handler._cancelled = True
    result = handler.stream_to_console(console)

    assert result["status"] == "cancelled"


def test_cancel_keeps_reading_the_programs_last_output(console):
    """After a cancel the stderr block the program writes while it shuts down
    (BEGIN bar, text, END bar) is still received, up to the final status."""
    messages = [
        "========== BEGIN PROGRAM STDERR ==========",
        "KeyboardInterrupt",
        "=========== END PROGRAM STDERR ===========",
        _status(exit_code=None, status="cancelled"),
    ]

    lines: list[str] = []
    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler._cancelled = True
    result = handler.stream_to_console(console, on_line=lines.append, echo=False)

    assert lines == messages[:3]
    assert result["status"] == "cancelled"
    assert result["exit_code"] == -1


def test_cancel_grace_ends_the_wait_when_the_pi_stays_silent(console):
    """Without a final status the cancel returns once the grace period is over."""
    import time as _time

    from websocket import WebSocketTimeoutException

    class SilentWebSocket(FakeWebSocket):
        def recv(self):
            _time.sleep(0.01)
            raise WebSocketTimeoutException()

    ws = SilentWebSocket()
    handler = OutputHandler("ws://fake:1234", recv_timeout=0.01, cancel_grace=0.1)
    handler._cancelled = True
    with patch("raccoon_cli.client.output_handler.create_connection", return_value=ws):
        started = _time.monotonic()
        result = handler.stream_to_console(console)
        elapsed = _time.monotonic() - started

    assert result == {"status": "cancelled", "exit_code": -1}
    assert 0.1 <= elapsed < 2.0


def test_cancel_with_closed_connection_is_not_an_error(console, capsys):
    """The Pi may close the stream after a cancel; that is not 'connection lost'."""
    from websocket import WebSocketConnectionClosedException

    messages = ["last line", WebSocketConnectionClosedException()]
    lines: list[str] = []
    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler._cancelled = True
    result = handler.stream_to_console(console, on_line=lines.append, echo=False)

    assert lines == ["last line"]
    assert result["status"] == "cancelled"
    assert "Connection to Pi lost" not in capsys.readouterr().out


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
    messages = ["alpha", "beta", _status(exit_code=0)]

    lines: list[str] = []
    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler.stream_to_console(console, on_line=lines.append)

    assert lines == ["alpha", "beta"]


def test_echo_false_suppresses_print_but_keeps_on_line(console, capsys):
    """echo=False routes lines only to on_line (for a caller-owned live TUI)."""
    messages = ["alpha", "beta", _status(exit_code=0)]

    lines: list[str] = []
    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler.stream_to_console(console, on_line=lines.append, echo=False)

    # Callback still fires for every line…
    assert lines == ["alpha", "beta"]
    # …but nothing was printed to stdout underneath the caller's renderer.
    out = capsys.readouterr().out
    assert "alpha" not in out
    assert "beta" not in out


def test_echo_true_prints_lines(console, capsys):
    """echo=True (default) prints each output line to stdout."""
    messages = ["alpha", _status(exit_code=0)]

    gen = _make_handler(messages)
    handler, ws = next(gen)
    handler.stream_to_console(console)

    assert "alpha" in capsys.readouterr().out
