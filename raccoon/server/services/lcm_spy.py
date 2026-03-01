"""LCM spy service for capturing and streaming LCM messages."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# LCM is only available on the Pi - handle import gracefully
try:
    import lcm

    LCM_AVAILABLE = True
except ImportError:
    lcm = None
    LCM_AVAILABLE = False


# Try to import exlcm types for message decoding
EXLCM_TYPES: dict[str, Any] = {}
try:
    from raccoon_transport.types import exlcm

    # Discover all message types in the exlcm module
    for name in dir(exlcm):
        obj = getattr(exlcm, name)
        if hasattr(obj, "decode") and hasattr(obj, "_get_packed_fingerprint"):
            EXLCM_TYPES[name] = obj
    logger.info(f"Loaded {len(EXLCM_TYPES)} exlcm message types: {list(EXLCM_TYPES.keys())}")
except ImportError:
    logger.debug("exlcm module not available - message decoding disabled")


def decode_lcm_message(data: bytes, channel: str) -> Optional[dict]:
    """
    Try to decode an LCM message using known types.

    LCM messages have an 8-byte fingerprint at the start that identifies the type.
    We try to match against known exlcm types.

    Returns decoded dict or None if decoding fails.
    """
    if len(data) < 8 or not EXLCM_TYPES:
        return None

    # Extract fingerprint from message
    msg_fingerprint = struct.unpack(">q", data[:8])[0]

    # Try to find a matching type
    for type_name, type_class in EXLCM_TYPES.items():
        try:
            type_fingerprint = type_class._get_packed_fingerprint()
            if isinstance(type_fingerprint, bytes):
                type_fingerprint = struct.unpack(">q", type_fingerprint)[0]

            if msg_fingerprint == type_fingerprint:
                # Found matching type - decode it
                msg = type_class.decode(data)
                return _lcm_msg_to_dict(msg, type_name)
        except Exception:
            continue

    return None


def _lcm_msg_to_dict(msg: Any, type_name: str) -> dict:
    """Convert an LCM message object to a dictionary."""
    result = {"_type": type_name}

    # Get all public attributes that aren't methods
    for attr in dir(msg):
        if attr.startswith("_"):
            continue
        if attr in ("decode", "encode", "get_hash"):
            continue

        try:
            value = getattr(msg, attr)
            if callable(value):
                continue

            # Handle nested LCM types
            if hasattr(value, "__dict__") and hasattr(value, "encode"):
                result[attr] = _lcm_msg_to_dict(value, type(value).__name__)
            elif isinstance(value, (list, tuple)):
                # Handle arrays
                result[attr] = [
                    _lcm_msg_to_dict(v, type(v).__name__)
                    if hasattr(v, "encode")
                    else v
                    for v in value
                ]
            else:
                result[attr] = value
        except Exception:
            continue

    return result


class SpyStatus(str, Enum):
    """Spy session status."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class LcmMessage:
    """Captured LCM message."""

    channel: str
    timestamp: str  # ISO format
    timestamp_us: int  # Microseconds since epoch for precise timing
    data_base64: str  # Base64-encoded binary data
    data_size: int  # Size in bytes
    decoded: Optional[dict] = None  # Decoded message content (if available)

    def to_dict(self) -> dict:
        result = {
            "channel": self.channel,
            "timestamp": self.timestamp,
            "timestamp_us": self.timestamp_us,
            "data_base64": self.data_base64,
            "data_size": self.data_size,
        }
        if self.decoded is not None:
            result["decoded"] = self.decoded
        return result

    def to_jsonl(self) -> str:
        """Convert to JSON Lines format for recording."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_jsonl(cls, line: str) -> LcmMessage:
        """Parse from JSON Lines format."""
        data = json.loads(line)
        return cls(
            channel=data["channel"],
            timestamp=data["timestamp"],
            timestamp_us=data["timestamp_us"],
            data_base64=data["data_base64"],
            data_size=data["data_size"],
            decoded=data.get("decoded"),
        )


class LcmSpyService:
    """
    Service for spying on LCM traffic.

    Runs LCM subscription in a background thread, broadcasts messages
    to async subscribers via queues.
    """

    def __init__(self, recordings_dir: Path):
        self.recordings_dir = recordings_dir
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        self._status = SpyStatus.IDLE
        self._lcm = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Channel filtering
        self._channel_patterns: list[str] = [".*"]  # Default: all channels

        # Subscribers (async queues)
        self._subscribers: list[asyncio.Queue] = []
        self._subscriber_lock = threading.Lock()

        # Recording state
        self._recording_file: Optional[Path] = None
        self._recording_handle = None
        self._message_count = 0

        # Stats
        self._start_time: Optional[datetime] = None
        self._channels_seen: set[str] = set()
        self._error_message: Optional[str] = None

    @property
    def status(self) -> SpyStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status == SpyStatus.RUNNING

    @property
    def stats(self) -> dict:
        return {
            "status": self._status.value,
            "message_count": self._message_count,
            "channels_seen": sorted(self._channels_seen),
            "channel_patterns": self._channel_patterns,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "recording_file": str(self._recording_file) if self._recording_file else None,
            "error": self._error_message,
        }

    def start(
        self,
        channel_patterns: Optional[list[str]] = None,
        record_to: Optional[str] = None,
    ) -> dict:
        """
        Start spying on LCM channels.

        Args:
            channel_patterns: List of channel patterns (fnmatch style).
                              None or empty means all channels (".*")
            record_to: Optional filename for recording (stored in recordings_dir)

        Returns:
            Dict with status, recording_file, etc.
        """
        if not LCM_AVAILABLE:
            return {
                "error": "LCM library not available on this system",
                "status": SpyStatus.ERROR.value,
            }

        if self.is_running:
            return {"error": "Spy already running", "status": self._status.value}

        self._channel_patterns = channel_patterns if channel_patterns else [".*"]
        self._message_count = 0
        self._channels_seen = set()
        self._start_time = datetime.utcnow()
        self._error_message = None

        # Set up recording if requested
        if record_to:
            self._recording_file = self.recordings_dir / record_to
            if not self._recording_file.suffix:
                self._recording_file = self._recording_file.with_suffix(".jsonl")
            try:
                self._recording_handle = open(self._recording_file, "w")
            except OSError as e:
                return {
                    "error": f"Failed to open recording file: {e}",
                    "status": SpyStatus.ERROR.value,
                }
        else:
            self._recording_file = None
            self._recording_handle = None

        # Start LCM thread
        self._stop_event.clear()
        self._status = SpyStatus.RUNNING
        self._thread = threading.Thread(target=self._lcm_thread, daemon=True)
        self._thread.start()

        logger.info(
            f"LCM spy started: patterns={self._channel_patterns}, "
            f"recording={self._recording_file}"
        )

        return {
            "status": self._status.value,
            "channel_patterns": self._channel_patterns,
            "recording_file": str(self._recording_file) if self._recording_file else None,
        }

    def stop(self) -> dict:
        """Stop the spy session."""
        if not self.is_running:
            return {"status": self._status.value, **self.stats}

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._recording_handle:
            self._recording_handle.close()
            self._recording_handle = None

        self._status = SpyStatus.STOPPED

        logger.info(
            f"LCM spy stopped: {self._message_count} messages, "
            f"channels={list(self._channels_seen)}"
        )

        return {
            "status": self._status.value,
            "message_count": self._message_count,
            "channels_seen": sorted(self._channels_seen),
            "recording_file": str(self._recording_file) if self._recording_file else None,
        }

    def _lcm_thread(self) -> None:
        """Background thread running LCM event loop."""
        try:
            self._lcm = lcm.LCM()

            # Subscribe to all channels, filter in handler
            self._lcm.subscribe(".*", self._on_message)

            while not self._stop_event.is_set():
                # Handle with timeout for responsive shutdown
                self._lcm.handle_timeout(100)  # 100ms timeout

        except Exception as e:
            logger.exception("LCM spy thread error")
            self._status = SpyStatus.ERROR
            self._error_message = str(e)
            self._broadcast_error(str(e))
        finally:
            self._lcm = None

    def _on_message(self, channel: str, data: bytes) -> None:
        """LCM message callback - runs in LCM thread."""
        # Check channel filter
        if not self._matches_filter(channel):
            return

        timestamp = datetime.utcnow()

        # Try to decode the message
        decoded = decode_lcm_message(data, channel)

        msg = LcmMessage(
            channel=channel,
            timestamp=timestamp.isoformat(),
            timestamp_us=int(timestamp.timestamp() * 1_000_000),
            data_base64=base64.b64encode(data).decode("ascii"),
            data_size=len(data),
            decoded=decoded,
        )

        self._message_count += 1
        self._channels_seen.add(channel)

        # Record if enabled
        if self._recording_handle:
            try:
                self._recording_handle.write(msg.to_jsonl() + "\n")
                self._recording_handle.flush()
            except OSError:
                pass  # Ignore write errors

        # Broadcast to subscribers
        self._broadcast_message(msg)

    def _matches_filter(self, channel: str) -> bool:
        """Check if channel matches any of the filter patterns."""
        for pattern in self._channel_patterns:
            if pattern == ".*" or fnmatch.fnmatch(channel, pattern):
                return True
        return False

    def _broadcast_message(self, msg: LcmMessage) -> None:
        """Broadcast message to all async subscribers."""
        with self._subscriber_lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait({"type": "message", **msg.to_dict()})
                except asyncio.QueueFull:
                    pass  # Drop message if subscriber is too slow

    def _broadcast_error(self, error: str) -> None:
        """Broadcast error to all subscribers."""
        with self._subscriber_lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait({"type": "error", "error": error})
                except asyncio.QueueFull:
                    pass

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        """Subscribe to message stream. Returns queue for receiving messages."""
        queue = asyncio.Queue(maxsize=maxsize)
        with self._subscriber_lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from message stream."""
        with self._subscriber_lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)


class LcmPlaybackService:
    """Service for replaying recorded LCM traffic."""

    def __init__(self, recordings_dir: Path):
        self.recordings_dir = recordings_dir
        self._status = SpyStatus.IDLE
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._progress: dict = {}
        self._error_message: Optional[str] = None

    @property
    def status(self) -> SpyStatus:
        return self._status

    @property
    def progress(self) -> dict:
        return {**self._progress, "error": self._error_message}

    def list_recordings(self) -> list[dict]:
        """List available recordings."""
        recordings = []
        if not self.recordings_dir.exists():
            return recordings

        for f in self.recordings_dir.glob("*.jsonl"):
            try:
                stat = f.stat()
                # Count lines to get message count
                with open(f) as fh:
                    line_count = sum(1 for line in fh if line.strip())
                recordings.append(
                    {
                        "filename": f.name,
                        "size_bytes": stat.st_size,
                        "message_count": line_count,
                        "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    }
                )
            except OSError:
                continue

        return sorted(recordings, key=lambda r: r["modified_at"], reverse=True)

    def get_recording_path(self, filename: str) -> Optional[Path]:
        """Get full path for a recording filename."""
        path = self.recordings_dir / filename
        if path.exists() and path.is_file():
            return path
        return None

    def delete_recording(self, filename: str) -> bool:
        """Delete a recording file."""
        path = self.get_recording_path(filename)
        if path:
            path.unlink()
            return True
        return False

    def start_playback(
        self,
        filename: str,
        speed: float = 1.0,
        loop: bool = False,
        channel_filter: Optional[list[str]] = None,
    ) -> dict:
        """
        Start playback of a recording.

        Args:
            filename: Recording filename
            speed: Playback speed multiplier (1.0 = realtime)
            loop: Whether to loop playback
            channel_filter: Optional channel patterns to filter

        Returns:
            Status dict
        """
        if not LCM_AVAILABLE:
            return {
                "error": "LCM library not available on this system",
                "status": SpyStatus.ERROR.value,
            }

        if self._status == SpyStatus.RUNNING:
            return {"error": "Playback already running"}

        recording_path = self.get_recording_path(filename)
        if not recording_path:
            return {"error": f"Recording not found: {filename}"}

        self._stop_event.clear()
        self._status = SpyStatus.RUNNING
        self._error_message = None
        self._progress = {
            "filename": filename,
            "messages_played": 0,
            "total_messages": 0,
            "speed": speed,
            "loop": loop,
        }

        self._thread = threading.Thread(
            target=self._playback_thread,
            args=(recording_path, speed, loop, channel_filter or []),
            daemon=True,
        )
        self._thread.start()

        logger.info(f"LCM playback started: {filename} at {speed}x speed")

        return {"status": "started", "filename": filename, "speed": speed, "loop": loop}

    def stop_playback(self) -> dict:
        """Stop current playback."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._status = SpyStatus.STOPPED

        logger.info(f"LCM playback stopped: {self._progress}")

        return {"status": "stopped", **self._progress}

    def _playback_thread(
        self,
        recording_path: Path,
        speed: float,
        loop: bool,
        channel_filter: list[str],
    ) -> None:
        """Background thread for playback."""
        try:
            lc = lcm.LCM()

            while not self._stop_event.is_set():
                messages = []
                with open(recording_path) as f:
                    for line in f:
                        if line.strip():
                            try:
                                messages.append(LcmMessage.from_jsonl(line))
                            except (json.JSONDecodeError, KeyError):
                                continue  # Skip invalid lines

                self._progress["total_messages"] = len(messages)

                if not messages:
                    logger.warning(f"Recording has no valid messages: {recording_path}")
                    break

                prev_ts = messages[0].timestamp_us

                for i, msg in enumerate(messages):
                    if self._stop_event.is_set():
                        break

                    # Filter channels
                    if channel_filter:
                        if not any(
                            fnmatch.fnmatch(msg.channel, p) for p in channel_filter
                        ):
                            continue

                    # Wait for appropriate time delta
                    delta_us = msg.timestamp_us - prev_ts
                    if delta_us > 0 and speed > 0:
                        wait_sec = (delta_us / 1_000_000) / speed
                        if wait_sec > 0.001:  # Only sleep if > 1ms
                            if self._stop_event.wait(wait_sec):
                                break  # Stop requested during wait
                    prev_ts = msg.timestamp_us

                    # Publish message
                    data = base64.b64decode(msg.data_base64)
                    lc.publish(msg.channel, data)

                    self._progress["messages_played"] = i + 1

                if not loop:
                    break

            self._status = SpyStatus.STOPPED

        except Exception as e:
            logger.exception("LCM playback thread error")
            self._status = SpyStatus.ERROR
            self._error_message = str(e)


# Global service instances
_spy_service: Optional[LcmSpyService] = None
_playback_service: Optional[LcmPlaybackService] = None


def get_spy_service(recordings_dir: Path) -> LcmSpyService:
    """Get or create the global spy service instance."""
    global _spy_service
    if _spy_service is None:
        _spy_service = LcmSpyService(recordings_dir)
    return _spy_service


def get_playback_service(recordings_dir: Path) -> LcmPlaybackService:
    """Get or create the global playback service instance."""
    global _playback_service
    if _playback_service is None:
        _playback_service = LcmPlaybackService(recordings_dir)
    return _playback_service
