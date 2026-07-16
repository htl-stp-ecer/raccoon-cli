"""Run-scoped sensor recorder: tail raccoon_ring SHM channels to an MCAP file.

The stm32-data-reader publishes every sensor value over the ``raccoon_ring``
shared-memory bus — one ``/dev/shm/raccoon_ring_<channel>`` file per channel,
NOT LCM-on-the-wire — so neither ``lcm-logger`` nor an LCM subscriber can see
this traffic. This recorder mmaps the ring files directly, does a seqlock read
of each new frame, decodes the big-endian payload codec, and appends the frame
to an MCAP file (zstd chunk compression) so a run's raw sensor stream (which
analog/gyro/… value at which time) can be downloaded and inspected in Foxglove.

It is spawned per run by ``raccoon run`` (see ``commands/run.py``) for the
duration of the mission and stopped with SIGTERM, at which point it finalises
the MCAP index. Only sensor channels are recorded — camera/YOLO/screen frames
(the only multi-MB messages) are deliberately excluded to keep files small.

Ring format (from ``raccoon-transport/cpp/src/raccoon_ring.c``): a 256-byte
native-little-endian header followed by ``slot_count`` slots. Payloads use the
raccoon big-endian codec (``int64 timestamp`` first, then fields) with no LCM
fingerprint prefix.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import signal
import struct
import sys
import time
from typing import Iterator, Optional

# --- Ring binary layout (native little-endian header) -----------------------

_HEADER_SIZE = 256
_MAGIC = 0x52435242  # "RCRB"
_OFF_MAGIC = 0
_OFF_SLOT_COUNT = 8
_OFF_SLOT_SIZE = 12
_OFF_MAX_PAYLOAD = 16
_OFF_PRODUCER_SEQ = 32
_SLOT_SEQ = 0
_SLOT_LEN = 8
_SLOT_DATA = 16

_SHM_PREFIX = "/dev/shm/raccoon_ring_"


def _shm_path(channel: str) -> str:
    """Map a channel name to its ring file path (``/`` -> ``_2F``)."""
    return _SHM_PREFIX + channel.replace("/", "_2F")


# --- Payload decoders: (struct format, field names after timestamp) ---------
# All big-endian; first field is always int64 timestamp (microseconds).
_DECODERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "vector3f": (">qfff", ("x", "y", "z")),
    "quaternion": (">qffff", ("w", "x", "y", "z")),
    "scalar_f": (">qf", ("value",)),
    "scalar_i32": (">qi", ("value",)),
    "scalar_i8": (">qb", ("value",)),
}

# Minimal JSON schemas per type for the MCAP schema registry.
_JSON_SCHEMAS: dict[str, dict] = {
    typ: {
        "type": "object",
        "properties": {
            "t": {"type": "integer"},
            **{name: {"type": "number"} for name in fields},
        },
    }
    for typ, (_fmt, fields) in _DECODERS.items()
}


def _decode(typ: str, payload: bytes) -> Optional[dict]:
    fmt, names = _DECODERS[typ]
    size = struct.calcsize(fmt)
    if len(payload) < size:
        return None
    unpacked = struct.unpack(fmt, payload[:size])
    frame = {"t": unpacked[0]}
    for name, value in zip(names, unpacked[1:]):
        frame[name] = value
    return frame


# --- Default channel whitelist (sensor data only; no camera/screen/yolo) ----

def _default_channels() -> list[tuple[str, str]]:
    """Return the default (channel, type) whitelist, with port expansion.

    This enumerates EVERY ``raccoon/`` channel the reader publishes as numeric
    sensor/state data (``stm32-data-reader`` ``DataPublisher``/``SystemMonitor``),
    so a run's ``sensors.mcap`` captures the full outbound bus. Deliberately
    excluded: command channels (``*_cmd`` — inbound, the reader subscribes to
    them, not publishes), ``raccoon/errors`` (a ``string_t``, not one of the five
    numeric codecs), and camera/YOLO/screen frames (the only multi-MB messages).

    Port counts are fixed by the reader (never more, never fewer): see
    ``wombat::DeviceTypes`` — ``MAX_ANALOG_PORTS=6`` (analog 0..5),
    ``MAX_MOTOR_PORTS=4`` (bemf/motor 0..3), ``MAX_SERVO_PORTS=4`` (servo 0..3) —
    and DataPublisher's hardcoded ``bit < 11`` digital loop (digital 0..10). So
    the exact channel set is enumerated, not probed.
    """
    channels: list[tuple[str, str]] = [
        # 3-axis vectors
        ("raccoon/gyro/value", "vector3f"),
        ("raccoon/accel/value", "vector3f"),
        ("raccoon/linear_accel/value", "vector3f"),
        ("raccoon/accel_velocity/value", "vector3f"),
        ("raccoon/mag/value", "vector3f"),
        # orientation
        ("raccoon/imu/quaternion", "quaternion"),
        # float scalars
        ("raccoon/imu/heading", "scalar_f"),
        ("raccoon/imu/temp/value", "scalar_f"),
        ("raccoon/battery/voltage", "scalar_f"),
        ("raccoon/cpu/temp/value", "scalar_f"),
        ("raccoon/odometry/pos_x", "scalar_f"),
        ("raccoon/odometry/pos_y", "scalar_f"),
        ("raccoon/odometry/heading", "scalar_f"),
        ("raccoon/odometry/vx", "scalar_f"),
        ("raccoon/odometry/vy", "scalar_f"),
        ("raccoon/odometry/wz", "scalar_f"),
        # heading-fusion debug (string-literal channels, not in Channels.h)
        ("raccoon/debug/fused_heading", "scalar_f"),
        ("raccoon/debug/yaw_rate", "scalar_f"),
        ("raccoon/debug/yaw_bias", "scalar_f"),
        # int32 state
        ("raccoon/debug/resting", "scalar_i32"),
        ("raccoon/system/shutdown_status", "scalar_i32"),
        ("raccoon/feature/bemf_enabled", "scalar_i32"),
        # int8 IMU accuracy (BNO08x calibration status 0..3)
        ("raccoon/gyro/accuracy", "scalar_i8"),
        ("raccoon/accel/accuracy", "scalar_i8"),
        ("raccoon/mag/accuracy", "scalar_i8"),
        ("raccoon/imu/quaternion_accuracy", "scalar_i8"),
    ]
    for port in range(6):  # MAX_ANALOG_PORTS
        channels.append((f"raccoon/analog/{port}/value", "scalar_i32"))
    for bit in range(11):  # DataPublisher digital loop: bit < 11
        channels.append((f"raccoon/digital/{bit}/value", "scalar_i32"))
    for port in range(4):  # MAX_MOTOR_PORTS
        channels.append((f"raccoon/bemf/{port}/value", "scalar_i32"))
        channels.append((f"raccoon/motor/{port}/position", "scalar_i32"))
        channels.append((f"raccoon/motor/{port}/power", "scalar_i32"))
        channels.append((f"raccoon/motor/{port}/done", "scalar_i32"))
    for port in range(4):  # MAX_SERVO_PORTS
        channels.append((f"raccoon/servo/{port}/position", "scalar_f"))
        channels.append((f"raccoon/servo/{port}/mode", "scalar_i8"))
    return channels


def _parse_channels_arg(spec: str) -> list[tuple[str, str]]:
    """Parse ``chan:type,chan:type`` into a list of (channel, type)."""
    out: list[tuple[str, str]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        chan, _, typ = item.partition(":")
        if typ not in _DECODERS:
            raise ValueError(f"unknown type '{typ}' for channel '{chan}'")
        out.append((chan, typ))
    return out


# --- Ring reader (seqlock) --------------------------------------------------

class _RingReader:
    """mmap a single ring file and yield new frames via a seqlock read."""

    def __init__(self, path: str):
        self.path = path
        self._fd = os.open(path, os.O_RDONLY)
        size = os.fstat(self._fd).st_size
        self._mm = mmap.mmap(self._fd, size, prot=mmap.PROT_READ)
        magic = struct.unpack_from("<I", self._mm, _OFF_MAGIC)[0]
        if magic != _MAGIC:
            self.close()
            raise ValueError(f"bad ring magic {magic:#x} in {path}")
        self.slot_count = struct.unpack_from("<I", self._mm, _OFF_SLOT_COUNT)[0]
        self.slot_size = struct.unpack_from("<I", self._mm, _OFF_SLOT_SIZE)[0]
        self.max_payload = struct.unpack_from("<I", self._mm, _OFF_MAX_PAYLOAD)[0]
        self.last_seen = 0
        self.dropped = 0

    def _producer_seq(self) -> int:
        return struct.unpack_from("<Q", self._mm, _OFF_PRODUCER_SEQ)[0]

    def _read_slot(self, seq: int) -> Optional[bytes]:
        idx = (seq - 1) % self.slot_count
        base = _HEADER_SIZE + idx * self.slot_size
        seq_before = struct.unpack_from("<Q", self._mm, base + _SLOT_SEQ)[0]
        if seq_before != seq:
            return None  # slot already overwritten or not yet written
        length = struct.unpack_from("<I", self._mm, base + _SLOT_LEN)[0]
        if length > self.max_payload:
            length = self.max_payload
        data = bytes(self._mm[base + _SLOT_DATA:base + _SLOT_DATA + length])
        seq_after = struct.unpack_from("<Q", self._mm, base + _SLOT_SEQ)[0]
        if seq_after != seq:
            return None  # torn read — producer overwrote mid-copy
        return data

    def poll(self) -> Iterator[bytes]:
        """Yield payloads for every new frame since the previous poll."""
        producer = self._producer_seq()
        if producer == self.last_seen:
            return
        if producer < self.last_seen:
            self.last_seen = 0  # producer restarted (new epoch)
        start = self.last_seen + 1
        if producer - self.last_seen > self.slot_count - 1:
            # Fell behind by more than the ring holds — skip to the oldest
            # still-intact frame and count the gap.
            new_start = producer - (self.slot_count - 1)
            self.dropped += new_start - start
            start = new_start
        for seq in range(start, producer + 1):
            payload = self._read_slot(seq)
            if payload is not None:
                yield payload
        self.last_seen = producer

    def close(self) -> None:
        try:
            self._mm.close()
        except Exception:
            pass
        try:
            os.close(self._fd)
        except Exception:
            pass


# --- Recorder main loop -----------------------------------------------------

_stop = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


def record(
    out_path: str,
    channels: list[tuple[str, str]],
    poll_hz: float,
    max_bytes: int,
    duration: float,
) -> int:
    try:
        from mcap.writer import CompressionType, Writer
    except ImportError:
        sys.stderr.write(
            "[sensor_recorder] mcap package not installed — skipping recording "
            "(install 'mcap' and 'zstandard')\n"
        )
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fh = open(out_path, "wb")
    writer = Writer(fh, compression=CompressionType.ZSTD)
    writer.start(profile="raccoon-sensors", library="raccoon-sensor-recorder")

    schema_ids: dict[str, int] = {}
    for typ, schema in _JSON_SCHEMAS.items():
        schema_ids[typ] = writer.register_schema(
            name=f"raccoon.{typ}",
            encoding="jsonschema",
            data=json.dumps(schema).encode("utf-8"),
        )

    chan_type = dict(channels)
    readers: dict[str, _RingReader] = {}
    channel_ids: dict[str, int] = {}
    seq_counters: dict[str, int] = {}
    warned: set[str] = set()

    def open_missing() -> None:
        for chan, typ in channels:
            if chan in readers:
                continue
            path = _shm_path(chan)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue  # ring not created yet — retry on next rescan
            if size < _HEADER_SIZE:
                # A 0-byte / partial placeholder: the ring isn't initialised
                # (e.g. an unpopulated port beyond the robot's actual count).
                # Skip silently and retry later in case it fills in.
                continue
            try:
                readers[chan] = _RingReader(path)
            except Exception as exc:
                if chan not in warned:
                    warned.add(chan)
                    sys.stderr.write(f"[sensor_recorder] open {chan} failed: {exc}\n")
                continue
            channel_ids[chan] = writer.register_channel(
                topic=chan,
                message_encoding="json",
                schema_id=schema_ids[typ],
            )
            seq_counters[chan] = 0

    interval = 1.0 / poll_hz if poll_hz > 0 else 0.004
    start_time = time.monotonic()
    next_rescan = 0.0
    next_size_check = 0.0
    message_count = 0

    try:
        while not _stop:
            loop_start = time.monotonic()
            if loop_start >= next_rescan:
                open_missing()
                next_rescan = loop_start + 2.0
            if duration > 0 and loop_start - start_time >= duration:
                break

            now = time.time_ns()
            for chan, reader in readers.items():
                typ = chan_type[chan]
                for payload in reader.poll():
                    frame = _decode(typ, payload)
                    if frame is None:
                        continue
                    writer.add_message(
                        channel_id=channel_ids[chan],
                        log_time=now,
                        data=json.dumps(frame).encode("utf-8"),
                        publish_time=now,
                        sequence=seq_counters[chan],
                    )
                    seq_counters[chan] += 1
                    message_count += 1

            if loop_start >= next_size_check:
                next_size_check = loop_start + 2.0
                try:
                    if max_bytes > 0 and fh.tell() >= max_bytes:
                        sys.stderr.write(
                            f"[sensor_recorder] max-bytes {max_bytes} reached, "
                            "stopping\n"
                        )
                        break
                except Exception:
                    pass

            sleep_for = interval - (time.monotonic() - loop_start)
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        for reader in readers.values():
            reader.close()
        try:
            writer.finish()
        finally:
            fh.close()

    dropped = sum(r.dropped for r in readers.values())
    sys.stderr.write(
        f"[sensor_recorder] wrote {message_count} messages from "
        f"{len(readers)} channels to {out_path}"
        + (f" ({dropped} frames dropped)" if dropped else "")
        + "\n"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output .mcap path")
    parser.add_argument(
        "--preset", default="default",
        help="channel preset ('default' = sensor whitelist)",
    )
    parser.add_argument(
        "--channels", default=None,
        help="explicit 'chan:type,chan:type' list (overrides --preset)",
    )
    parser.add_argument("--poll-hz", type=float, default=250.0)
    parser.add_argument(
        "--max-bytes", type=int, default=200 * 1024 * 1024,
        help="stop after the file reaches this size (0 = unlimited)",
    )
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="stop after this many seconds (0 = until SIGTERM)",
    )
    args = parser.parse_args(argv)

    if args.channels:
        channels = _parse_channels_arg(args.channels)
    else:
        channels = _default_channels()

    return record(
        out_path=args.out,
        channels=channels,
        poll_hz=args.poll_hz,
        max_bytes=args.max_bytes,
        duration=args.duration,
    )


if __name__ == "__main__":
    raise SystemExit(main())
