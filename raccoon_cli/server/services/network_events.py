"""Pushes cable and mode changes to the UI without it having to poll.

The UI asks `GET /network/lan/events?since=<version>` and the server answers
as soon as something changed (or after a timeout). A watcher thread reads the
kernel's link and address events (`ip monitor link address`), so a plugged or
pulled cable, a new address or a Wi-Fi connection shows up within a fraction of
a second and costs nothing while nothing happens.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from typing import Any, Callable


class NetworkEvents:
    """Latest network state with a version that grows on every change."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._version = 0
        self._state: dict[str, Any] = {}
        self._provider: Callable[[], dict[str, Any]] | None = None
        # Clients waiting without a thread: (their event loop, their future).
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    def set_provider(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._provider = provider

    def publish(self, state: dict[str, Any]) -> bool:
        """Store [state]; wake waiting clients if it differs. True if it did."""
        with self._cond:
            if state == self._state:
                return False
            self._state = dict(state)
            self._version += 1
            self._cond.notify_all()
            for loop, future in self._waiters:
                loop.call_soon_threadsafe(_wake, future)
            return True

    def refresh(self) -> None:
        """Read the current state and publish it (after a mode change)."""
        if self._provider is None:
            return
        try:
            self.publish(self._provider())
        except Exception as exc:
            print(f"Network state refresh failed: {exc}")

    def wait(self, since: int, timeout: float) -> dict[str, Any]:
        """The state once its version differs from [since], or after [timeout].

        A client that passes -1 (or a version from before a server restart)
        gets the current state at once.
        """
        with self._cond:
            if self._version == since:
                self._cond.wait_for(lambda: self._version != since, timeout)
            return {"version": self._version, **self._state}

    async def wait_async(self, since: int, timeout: float) -> dict[str, Any]:
        """Like [wait], but for the server's event loop: no thread is blocked."""
        loop = asyncio.get_running_loop()
        with self._cond:
            if self._version != since:
                return {"version": self._version, **self._state}
            future: asyncio.Future = loop.create_future()
            self._waiters.append((loop, future))
        try:
            await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._cond:
                self._waiters = [w for w in self._waiters if w[1] is not future]
        with self._cond:
            return {"version": self._version, **self._state}

    def run_watcher(self, stop: threading.Event, interval: float = 30.0) -> None:
        """Keep the state up to date until [stop] is set.

        Wakes on kernel link events and, as a safety net, every [interval]
        seconds (in case `ip monitor` is missing or died).
        """
        dirty = threading.Event()
        monitor = self._start_link_monitor(dirty, stop)
        try:
            self.refresh()
            while not stop.is_set():
                dirty.wait(interval)
                if stop.is_set():
                    break
                # Several events arrive together (link down/up, carrier);
                # let them settle, then read the state once.
                stop.wait(0.3)
                dirty.clear()
                self.refresh()
        finally:
            if monitor is not None:
                monitor.terminate()

    @staticmethod
    def _start_link_monitor(
        dirty: threading.Event, stop: threading.Event
    ) -> subprocess.Popen | None:
        try:
            process = subprocess.Popen(
                ["ip", "-o", "monitor", "link", "address"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            print(f"Link monitor unavailable, falling back to slow checks: {exc}")
            return None

        def read_events() -> None:
            assert process.stdout is not None
            for _line in process.stdout:
                if stop.is_set():
                    break
                dirty.set()

        threading.Thread(target=read_events, daemon=True).start()
        return process


def _wake(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


# One hub for the whole server: the routes and the watcher share it.
network_events = NetworkEvents()
