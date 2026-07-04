"""Sidecar metadata cache for completed log runs.

Parsing a multi-megabyte run file just to render a one-line summary is the
bottleneck in ``raccoon logs`` — on a Pi it costs roughly a second per file,
so a 25-run listing blows past the HTTP client's 30s timeout.

Completed run files never change, so each run's summary (times, line/level/
source counts) is cached in a hidden JSON sidecar next to the log file. The
sidecar is validated against the log file's size + mtime, so a file that is
still being written (the live run) invalidates its own cache naturally — its
size/mtime keep changing, so it's simply re-parsed until it settles.

Caching is a best-effort optimization: any read/parse/write error falls back
to a normal full parse. Nothing here is load-bearing for correctness.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from .parser import LogRun

#: Bump when the cached payload shape changes so stale sidecars are ignored.
CACHE_VERSION = 1
_CACHE_SUFFIX = ".meta.json"


def cache_path(log_file: Path) -> Path:
    """Sidecar path for *log_file* — hidden, so it never matches the run glob.

    ``libstp-2026-07-04_12-15-58.log`` →
    ``.libstp-2026-07-04_12-15-58.log.meta.json``
    """
    return log_file.with_name(f".{log_file.name}{_CACHE_SUFFIX}")


def load_cached_run(log_file: Path) -> Optional[LogRun]:
    """Return a summary-only :class:`LogRun` from the sidecar, or None.

    None means "no usable cache" — the caller should parse the file. A cache
    is usable only if it exists, is well-formed, matches ``CACHE_VERSION``, and
    the recorded size + mtime still match the log file on disk.
    """
    cache = cache_path(log_file)
    try:
        st = log_file.stat()
        raw = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return None
    if raw.get("size") != st.st_size or raw.get("mtime_ns") != st.st_mtime_ns:
        return None

    try:
        return LogRun(
            index=0,  # re-indexed by load_runs after the full set is gathered
            start_time=datetime.fromisoformat(raw["start_time"]),
            end_time=datetime.fromisoformat(raw["end_time"]),
            duration_secs=float(raw["duration_secs"]),
            entries=[],
            file_path=str(log_file),
            summary_line_count=int(raw["line_count"]),
            summary_level_counts={str(k): int(v) for k, v in raw["level_counts"].items()},
            summary_sources=set(raw["sources"]),
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def write_cached_run(log_file: Path, run: LogRun) -> None:
    """Write *run*'s summary to the sidecar. Best-effort — errors are ignored.

    Written atomically (temp file + rename) so a concurrent reader never sees a
    half-written sidecar.
    """
    cache = cache_path(log_file)
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    try:
        st = log_file.stat()
        payload = {
            "version": CACHE_VERSION,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "start_time": run.start_time.isoformat(),
            "end_time": run.end_time.isoformat(),
            "duration_secs": run.duration_secs,
            "line_count": run.line_count,
            "level_counts": run.level_counts,
            "sources": sorted(run.sources),
        }
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache)  # atomic within the same directory
    except OSError:
        # Read-only fs, race, disk full, … — caching is optional, keep going.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
