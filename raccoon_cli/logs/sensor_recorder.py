"""Run-scoped sensor recorder: subscribe to the sensor channels and write an MCAP file.

The stm32-data-reader publishes every sensor value on the shared-memory transport of
raccoon-transport, so neither ``lcm-logger`` nor an LCM subscriber can see this traffic. This
recorder subscribes to the channels through ``raccoon_transport`` (the Python binding of the C++
transport), decodes the big-endian payload codec and appends each frame to an MCAP file (zstd
chunk compression), so a run's raw sensor stream (which analog/gyro/... value at which time) can
be downloaded and inspected in Foxglove. A retained channel delivers its latest value when the
recording starts.

It is spawned per run by ``raccoon run`` (see ``commands/run.py``) for the duration of the
mission and stopped with SIGTERM, at which point it finalises the MCAP index. Only sensor
channels are recorded: camera and screen frames (the only multi-MB messages) are excluded to
keep files small.
"""

from __future__ import annotations

import argparse
import json
import queue
import signal
import struct
import sys
import time
from typing import Optional

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

    These are the numeric sensor and state channels of the channel table that the
    stm32-data-reader publishes. Deliberately excluded: command channels (inbound),
    ``messages/error`` and ``messages/warn`` (``string_t``, not one of the five numeric codecs),
    and camera and screen frames.

    Port counts are fixed by the reader: analog 0..5, digital 0..10, motors 0..3, servos 0..3.
    """
    from raccoon_transport import channels

    imu = channels.hardware.imu.state
    odometry = channels.hardware.odometry.state
    result: list[tuple[str, str]] = [
        (imu.GYRO, "vector3f"),
        (imu.ACCEL, "vector3f"),
        (imu.LINEAR_ACCEL, "vector3f"),
        (imu.ACCEL_VELOCITY, "vector3f"),
        (imu.MAG, "vector3f"),
        (imu.QUATERNION, "quaternion"),
        (imu.HEADING, "scalar_f"),
        (imu.TEMPERATURE, "scalar_f"),
        (channels.hardware.battery.state.VOLTAGE, "scalar_f"),
        (channels.system.state.CPU_TEMPERATURE, "scalar_f"),
        (odometry.POS_X, "scalar_f"),
        (odometry.POS_Y, "scalar_f"),
        (odometry.HEADING, "scalar_f"),
        (odometry.VX, "scalar_f"),
        (odometry.VY, "scalar_f"),
        (odometry.WZ, "scalar_f"),
        (channels.hardware.latch.state.FLAGS, "scalar_i32"),
        (channels.hardware.feature.state.BEMF_ACTIVE, "scalar_i32"),
        (imu.GYRO_ACCURACY, "scalar_i8"),
        (imu.ACCEL_ACCURACY, "scalar_i8"),
        (imu.MAG_ACCURACY, "scalar_i8"),
        (imu.QUATERNION_ACCURACY, "scalar_i8"),
    ]
    for port in range(6):
        result.append((channels.hardware.analog.state.value(port), "scalar_i32"))
    for bit in range(11):
        result.append((channels.hardware.digital.state.value(bit), "scalar_i32"))
    for port in range(4):
        motor = channels.hardware.motor.state
        result.append((motor.bemf(port), "scalar_i32"))
        result.append((motor.position(port), "scalar_i32"))
        result.append((motor.power(port), "scalar_i32"))
    for port in range(4):
        servo = channels.hardware.servo.state
        result.append((servo.position(port), "scalar_f"))
        result.append((servo.mode(port), "scalar_i8"))
    return result


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


# --- Recorder main loop -----------------------------------------------------

_stop = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


def record(
    out_path: str,
    channels: list[tuple[str, str]],
    poll_hz: float,  # kept for the command line; frames arrive on the transport's thread
    max_bytes: int,
    duration: float,
) -> int:
    import os

    try:
        from mcap.writer import CompressionType, Writer
    except ImportError:
        sys.stderr.write(
            "[sensor_recorder] mcap package not installed — skipping recording "
            "(install 'mcap' and 'zstandard')\n"
        )
        return 0
    try:
        import raccoon_transport
    except ImportError:
        sys.stderr.write("[sensor_recorder] raccoon_transport not installed — skipping recording\n")
        return 0
    if not raccoon_transport.NATIVE:
        sys.stderr.write("[sensor_recorder] raccoon_transport has no native module — skipping recording\n")
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

    # The transport calls the handlers on a thread of its own; they only queue the frame, and
    # this thread writes the file.
    frames: queue.SimpleQueue = queue.SimpleQueue()
    channel_ids: dict[str, int] = {}
    seq_counters: dict[str, int] = {}
    chan_type = dict(channels)
    transport = raccoon_transport.get_transport()
    for chan, typ in channels:
        channel_ids[chan] = writer.register_channel(
            topic=chan, message_encoding="json", schema_id=schema_ids[typ]
        )
        seq_counters[chan] = 0
        try:
            transport.subscribe(
                chan, lambda ch, data: frames.put((time.time_ns(), ch, data))
            )
        except RuntimeError as exc:
            sys.stderr.write(f"[sensor_recorder] subscribe {chan} failed: {exc}\n")

    start_time = time.monotonic()
    next_size_check = 0.0
    message_count = 0

    try:
        while not _stop:
            now_mono = time.monotonic()
            if duration > 0 and now_mono - start_time >= duration:
                break
            try:
                received_ns, chan, payload = frames.get(timeout=0.2)
            except queue.Empty:
                received_ns = None
            while received_ns is not None:
                frame = _decode(chan_type[chan], payload)
                if frame is not None:
                    writer.add_message(
                        channel_id=channel_ids[chan],
                        log_time=received_ns,
                        data=json.dumps(frame).encode("utf-8"),
                        publish_time=received_ns,
                        sequence=seq_counters[chan],
                    )
                    seq_counters[chan] += 1
                    message_count += 1
                try:
                    received_ns, chan, payload = frames.get_nowait()
                except queue.Empty:
                    received_ns = None

            if now_mono >= next_size_check:
                next_size_check = now_mono + 2.0
                try:
                    if max_bytes > 0 and fh.tell() >= max_bytes:
                        sys.stderr.write(f"[sensor_recorder] max-bytes {max_bytes} reached, stopping\n")
                        break
                except Exception:
                    pass
    finally:
        raccoon_transport.shutdown_transport()
        try:
            writer.finish()
        finally:
            fh.close()

    sys.stderr.write(
        f"[sensor_recorder] wrote {message_count} messages from {len(channels)} channels to {out_path}\n"
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
    parser.add_argument("--poll-hz", type=float, default=250.0, help="ignored, kept for old command lines")
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
