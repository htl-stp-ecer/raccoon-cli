"""STM32 data-reader command trace (``cmd_trace.jsonl``) reading and windowing.

The stm32-data-reader writes a receive-side command trace to a JSONL file when
its ``WOMBAT_CMD_TRACE`` environment variable names a writable path (the systemd
unit sets it to ``/home/pi/stm32_data_reader/cmd_trace.jsonl`` and truncates the
file on each reader restart). Each line records one motor/servo/chassis command
as its transport handler fires (stage ``recv``) and as a servo position is staged
into the SPI buffer for the STM32 (stage ``spi``).

Per-line JSON fields (see stm32-data-reader ``CmdTrace.h``):

    t_ns   steady-clock nanoseconds at the event (intra-process order)
    w_us   system wall-clock microseconds — the SAME POSIX clock the libstp log
           timestamps use, so this is the correlation key against a log run
    rseq   process-global monotonic counter (arrival/apply order)
    stage  "recv" (handler fired) or "spi" (staged to SPI buffer)
    kind   short command kind ("servo_pos", "motor_vel", ...)
    ch     channel name ("" for the "spi" stage, which is post-decode)
    port   device port, or -1 when not port-scoped
    v      command value (single scalar; the primary setpoint)
    ts_us  message timestamp echoed from the wire (0 when unavailable)

This module resolves the trace path, loads it, and slices it to a log run's time
window so a downloaded debug bundle carries exactly the commands the STM32 saw
while that mission ran.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

# Packaged default from the stm32-data-reader systemd unit. Used as a fallback
# when the running service's ``WOMBAT_CMD_TRACE`` can't be read.
DEFAULT_CMD_TRACE_PATH = Path("/home/pi/stm32_data_reader/cmd_trace.jsonl")

_STM32_SERVICE = "stm32_data_reader.service"
_CMD_TRACE_ENV = "WOMBAT_CMD_TRACE"


def resolve_cmd_trace_path() -> Path:
    """Best-effort path to the reader's ``cmd_trace.jsonl``.

    Prefers the ``WOMBAT_CMD_TRACE`` value from the ``stm32_data_reader`` systemd
    unit — the source of truth on the Pi — and falls back to the packaged default
    when systemd isn't reachable (e.g. reading a local copy off the robot).
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", _STM32_SERVICE, "--property=Environment"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return DEFAULT_CMD_TRACE_PATH

    line = proc.stdout.strip()
    # Format: ``Environment=WOMBAT_CMD_TRACE=/path OTHER=val`` (space-separated).
    if line.startswith("Environment="):
        for token in line[len("Environment=") :].split():
            if token.startswith(_CMD_TRACE_ENV + "="):
                value = token.split("=", 1)[1]
                if value:
                    return Path(value)
    return DEFAULT_CMD_TRACE_PATH


def datetime_to_us(dt: datetime) -> int:
    """POSIX microseconds for a naive local datetime.

    Log-run timestamps are naive local wall-clock (second resolution); the trace
    ``w_us`` is ``system_clock`` microseconds since the POSIX epoch. ``.timestamp()``
    interprets a naive datetime as local time, so both land on the same axis.
    """
    return int(dt.timestamp() * 1_000_000)


def run_window_us(
    start_time: datetime, end_time: datetime, pad_secs: float = 2.0
) -> Tuple[int, int]:
    """Return the ``[start_us, end_us]`` cmd_trace window for a run.

    *pad_secs* widens the window on both ends. Log timestamps are truncated to
    whole seconds and commands often bracket the first/last logged line, so a
    small pad avoids clipping the boundary commands most relevant to debugging.
    """
    pad_us = int(pad_secs * 1_000_000)
    return datetime_to_us(start_time) - pad_us, datetime_to_us(end_time) + pad_us


def load_cmd_trace(path: Path) -> List[dict]:
    """Parse ``cmd_trace.jsonl`` into a list of records; skips malformed lines.

    The reader flushes every 64 records, so a crash can leave a final partial
    line — those are silently dropped rather than failing the whole read.
    """
    records: List[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return records


def slice_cmd_trace(records: List[dict], start_us: int, end_us: int) -> List[dict]:
    """Keep records whose ``w_us`` falls within ``[start_us, end_us]`` inclusive."""
    out: List[dict] = []
    for r in records:
        w = r.get("w_us")
        if isinstance(w, (int, float)) and start_us <= w <= end_us:
            out.append(r)
    return out
