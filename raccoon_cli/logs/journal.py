"""Systemd journal collection for run debug bundles.

A downloaded run bundle carries the libstp mission log and the STM32 command
trace, but those alone don't explain failures that live in the *host* processes
driving the hardware. This module grabs the journald output — sliced to the
run's wall-clock window — of every service raccoon started or manages, so a
bundle tells the whole story offline:

* ``raccoon.service``            — the raccoon-server that ran the mission
* ``stm32_data_reader.service``  — the STM32 bridge that fed the hardware
* every project service declared in ``raccoon.project.yml`` (units named
  ``raccoon-project-<id>-<name>.service``) that raccoon starts alongside a run

Timestamp axis: journald keys on ``__REALTIME_TIMESTAMP`` (POSIX microseconds).
``cmd_trace.run_window_us`` already yields the padded ``[start_us, end_us]``
window in the same POSIX-microsecond axis the cmd_trace slice uses, so the two
sources line up. journalctl's ``--since``/``--until`` default to local time, and
``_us_to_stamp`` renders the window as a naive-local string to match — the same
``datetime.timestamp()`` round-trip ``cmd_trace.datetime_to_us`` relies on.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# Core services raccoon owns on every Pi, independent of the project config.
CORE_BUNDLE_UNITS: Tuple[Tuple[str, str], ...] = (
    ("raccoon-server", "raccoon.service"),
    ("stm32-data-reader", "stm32_data_reader.service"),
)

# journald PRIORITY → log level name (RFC 5424).
_JOURNAL_PRIORITY = {
    "0": "EMERG",
    "1": "ALERT",
    "2": "CRITICAL",
    "3": "ERROR",
    "4": "WARN",
    "5": "NOTICE",
    "6": "INFO",
    "7": "DEBUG",
}

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_label(label: str) -> str:
    """Sanitise a service label into a filename-safe slug."""
    slug = _SAFE_FILENAME.sub("-", label).strip("-")
    return slug or "service"


def journal_filename(label: str) -> str:
    """Bundle filename for a service journal (``journal.<label>.jsonl``)."""
    return f"journal.{_safe_label(label)}.jsonl"


def _us_to_stamp(us: int) -> str:
    """Naive-local ``YYYY-MM-DD HH:MM:SS`` for a POSIX-microsecond instant.

    journalctl's ``--since``/``--until`` interpret bare timestamps as local
    time, and ``cmd_trace.datetime_to_us`` built the window from local
    ``datetime.timestamp()`` — so rendering back through ``fromtimestamp`` keeps
    both ends of the correlation on the same clock.
    """
    return datetime.fromtimestamp(us / 1_000_000).strftime("%Y-%m-%d %H:%M:%S")


def parse_journal_json(stdout: str) -> List[dict]:
    """Parse ``journalctl -o json`` output into flat entry dicts.

    Each line is one journal record; malformed/partial lines are skipped. The
    ``__REALTIME_TIMESTAMP`` (POSIX microseconds, UTC) is rendered to an ISO
    string and ``PRIORITY`` mapped to a level name.
    """
    entries: List[dict] = []
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts_iso = ""
        ts_us = obj.get("__REALTIME_TIMESTAMP")
        if ts_us:
            try:
                ts_iso = datetime.fromtimestamp(
                    int(ts_us) / 1_000_000, tz=timezone.utc
                ).isoformat()
            except (ValueError, TypeError):
                ts_iso = ""
        entries.append(
            {
                "timestamp": ts_iso,
                "level": _JOURNAL_PRIORITY.get(str(obj.get("PRIORITY", "6")), "INFO"),
                "message": obj.get("MESSAGE", ""),
                "pid": obj.get("_PID", ""),
                "identifier": obj.get("SYSLOG_IDENTIFIER", ""),
            }
        )
    return entries


def journalctl_lines(unit: str, lines: int) -> List[dict]:
    """Fetch the last *lines* journal entries for *unit* as parsed dicts.

    Raises ``RuntimeError`` on a non-zero journalctl exit (used by the live
    service-journal endpoint, which surfaces the failure to the caller).
    """
    proc = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"journalctl exited {proc.returncode}"
        )
    return parse_journal_json(proc.stdout)


def journalctl_window(
    unit: str, start_us: int, end_us: int
) -> Tuple[List[dict], Optional[str]]:
    """Fetch *unit*'s journal entries within ``[start_us, end_us]``.

    Returns ``(entries, error)`` — ``error`` is ``None`` on success or a short
    reason string otherwise. Never raises, so one unreadable unit (or a Pi
    without journald access) doesn't sink the whole bundle.
    """
    since = _us_to_stamp(start_us)
    until = _us_to_stamp(end_us)
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                since,
                "--until",
                until,
                "--no-pager",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], str(exc)
    if proc.returncode != 0:
        return [], proc.stderr.strip() or f"journalctl exited {proc.returncode}"
    return parse_journal_json(proc.stdout), None


def bundle_journal_units(project_path: Optional[Path]) -> List[Tuple[str, str]]:
    """``(label, systemd_unit)`` pairs to bundle for a run.

    The two core raccoon services plus every service declared in the project's
    ``raccoon.project.yml`` (the ones raccoon starts and manages). Deduped by
    unit so a project can't accidentally re-list a core service.
    """
    units: List[Tuple[str, str]] = list(CORE_BUNDLE_UNITS)

    if project_path is not None:
        try:
            from raccoon_cli.project import load_project_config
            from raccoon_cli.project_services import load_project_services

            config = load_project_config(project_path)
            if isinstance(config, dict):
                for svc in load_project_services(config, project_path):
                    units.append((svc.name, svc.systemd_name))
        except Exception:
            # A missing/broken project config must not block the core journals.
            pass

    seen: set[str] = set()
    deduped: List[Tuple[str, str]] = []
    for label, unit in units:
        if unit in seen:
            continue
        seen.add(unit)
        deduped.append((label, unit))
    return deduped


def collect_journals(
    units: List[Tuple[str, str]], start_us: int, end_us: int
) -> List[dict]:
    """Collect a windowed journal slice per unit.

    Each returned section: ``label``, ``unit``, ``file``, ``available``,
    ``entry_count``, ``window_start_us``, ``window_end_us``, ``error``, and the
    raw ``entries`` list (stripped for the manifest via
    ``journal_manifest_section``).
    """
    sections: List[dict] = []
    for label, unit in units:
        entries, error = journalctl_window(unit, start_us, end_us)
        sections.append(
            {
                "label": label,
                "unit": unit,
                "file": journal_filename(label),
                "available": error is None,
                "entry_count": len(entries),
                "window_start_us": start_us,
                "window_end_us": end_us,
                "error": error,
                "entries": entries,
            }
        )
    return sections


def journal_manifest_section(section: dict) -> dict:
    """A journal section without its raw ``entries`` (for the manifest)."""
    return {k: v for k, v in section.items() if k != "entries"}


def write_journal_file(directory: Path, section: dict) -> int:
    """Write a journal section's entries as JSONL into *directory*; return size.

    Always writes the file (even when empty or unavailable) so the bundle is
    self-documenting about which services were queried.
    """
    path = directory / section["file"]
    with open(path, "w", encoding="utf-8") as f:
        for entry in section.get("entries", []):
            f.write(json.dumps(entry) + "\n")
    return path.stat().st_size


def journal_file_body(section: dict) -> str:
    """The JSONL body for a journal section (for zipping in memory)."""
    return "".join(json.dumps(entry) + "\n" for entry in section.get("entries", []))
