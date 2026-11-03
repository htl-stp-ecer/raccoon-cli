"""Transport contract status: the one service layer for the Pi server, the CLI and the Web-IDE.

The semantics (decision table, status files, aggregation) live in
``raccoon_transport.contract_check`` and ``contract/CHECK_SPEC.md`` of the
raccoon-transport repository. This module only adapts them for raccoon-cli:

* the Pi server records its own startup check and serves the aggregate,
* the server refuses to start a run while a component's contract does not match,
* the CLI (doctor, connect, run) shows what the server reports.

Nothing here talks over the transport: the status files exist exactly because the
transport may be the thing that is broken. A transport older than the contract
check is reported as unavailable and never blocks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

try:  # transport releases before the contract check do not have it
    from raccoon_transport import contract_check as _check
except ImportError:  # pragma: no cover - exercised through available()
    _check = None

SERVER_COMPONENT = "raccoon-server"
# The server is released as part of raccoon-cli, so that is its entry in the bundle.
SERVER_BUNDLE_KEY = "raccoon-cli"


def available() -> bool:
    return _check is not None


def unavailable_summary() -> dict[str, Any]:
    return {
        "available": False,
        "ok": True,
        "dev_mode": False,
        "hashes_differ": False,
        "expected_hash": None,
        "bundle": {"file": None, "error": None},
        "components": [],
        "blocking": [],
        "note": "the installed raccoon-transport has no contract check",
    }


def _normalize_version(value: str) -> str:
    """`v173` and `173` are the same version; nothing else is normalised."""
    value = value.strip()
    return value[1:] if value[:1] in ("v", "V") else value


def version_check(version: str, bundle_file: Optional[str] = None, *,
                  key: str = SERVER_BUNDLE_KEY, component: str = SERVER_COMPONENT):
    """A component's version against its entry of the bundle file (default: the server's ``raccoon-cli``).

    This is the server's own component check (specified in raccoon-transport,
    ``contract/COMPONENT_VERSION_CHECK.md``); the transport only merges its result.
    Returns None when the bundle file cannot be read (the transport check reports that).
    """
    if _check is None:
        return None
    path = bundle_file or os.environ.get("RACCOON_BUNDLE_FILE") or "/etc/raccoon-bundle.json"
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    components = document.get("components")
    entry = components.get(key) if isinstance(components, dict) else None
    bundle = document.get("bundle") if isinstance(document.get("bundle"), str) else None

    def result(outcome, message, reason=None, expected=None):
        return _check.ComponentCheck(name="version", result=outcome, message=message, reason=reason, expected=expected)

    if not isinstance(entry, str):
        return result("bundle-fehler", f"bundle check failed: bundle file has no version for {key}: {path}",
                      "version-field-missing")
    expected = entry.strip()
    if not expected:
        return result("bundle-fehler", f"bundle check failed: bundle file has an empty version for {key}: {path}",
                      "version-field-empty")
    if expected == "dev":
        return result(
            "dev-warnung",
            f"component version check omitted ('{key}' is 'dev' in the bundle): "
            f"{component} {version or 'unknown'} is not compared; mismatches are not enforced",
            "version-omitted", expected)
    if _normalize_version(expected) != _normalize_version(version):
        return result(
            "version-mismatch",
            f"component version mismatch: {component} is {version or 'unknown'} but bundle "
            f"{bundle or 'unknown'} expects {expected}; update or reflash so all components match",
            None, expected)
    return result("ok", f"version ok: {version}", None, expected)


def installed_version() -> str:
    """The raccoon-cli version installed right now, read from the package metadata.

    ``raccoon_cli.__version__`` is frozen at build time (a dev release says
    ``0.0``) and the server process keeps the value it imported at start, so an
    update or reinstall would never show. The metadata is re-read on every call.
    """
    from importlib import metadata

    try:
        return metadata.version("raccoon-cli")
    except metadata.PackageNotFoundError:
        from raccoon_cli import __version__

        return __version__


def refresh_server_start():
    """Re-run the server's own startup check with the version installed now."""
    return record_server_start(installed_version())


def record_server_start(version: str):
    """Run and record the server's own startup check. Never raises, never stops the server.

    The server must stay up even on a mismatch: it is the one that reports the
    problem. It refuses runs instead (see :func:`refusal_message`).
    """
    if _check is None:
        return None
    try:
        return _check.startup_check(
            SERVER_COMPONENT, version, persistent=True, enforce=False, component_check=version_check(version)
        )
    except Exception as exc:  # a broken check must not take the server down
        print(f"Transport contract check failed to run: {exc}")
        return None


def local_summary() -> dict[str, Any]:
    """What ``GET /api/v1/contract`` returns (read from this Pi's status files)."""
    if _check is None:
        return unavailable_summary()
    return {"available": True, **_check.aggregate()}


# Components that write a status file once at their own start. After an update their
# file still names the old version, so the version is read again from what is installed.
# (status component, bundle key, reader of the installed version)
def _live_botui() -> Optional[str]:
    from raccoon_cli.server.routes.version import _BOTUI_VERSION_FILE, _file_version

    return _file_version(_BOTUI_VERSION_FILE)


def _live_stm32() -> Optional[str]:
    from raccoon_cli.server.routes.version import _STM32_BINARY, _binary_version

    return _binary_version(_STM32_BINARY)


_LIVE_COMPONENTS = (
    ("botui", "botui", _live_botui),
    ("stm32-data-reader", "stm32-data-reader", _live_stm32),
)


def _recheck_entry(entry: dict[str, Any], key: str, live: str) -> dict[str, Any]:
    """The status entry with the version check redone for the version installed now.

    A failure of the transport check itself (hash mismatch, broken bundle) stays as it
    is: only a result that came from the version check is replaced.
    """
    old_check = entry.get("component_check") or {}
    from_version_check = bool(old_check) and entry.get("result") == old_check.get("result")
    transport_failed = _check.is_refusing(entry.get("result")) and not from_version_check
    fresh = version_check(live, key=key, component=entry["component"])
    updated = dict(entry, component_version=live)
    if fresh is None:
        return updated
    updated["component_check"] = {
        "name": fresh.name, "result": fresh.result, "message": fresh.message,
        "reason": fresh.reason, "expected": fresh.expected,
    }
    if not transport_failed:
        updated.update(result=fresh.result, reason=fresh.reason, message=fresh.message)
    return updated


def refresh_components(summary: dict[str, Any]) -> dict[str, Any]:
    """Redo the version check of the other components with the versions installed now."""
    if _check is None or not summary.get("available", True) or not summary.get("on_device", True):
        return summary
    live_by_name = {}
    for name, key, reader in _LIVE_COMPONENTS:
        try:
            live = reader()
        except Exception:
            live = None
        if live:
            live_by_name[name] = (key, live)
    if not live_by_name:
        return summary
    components = []
    changed = set()
    for entry in summary.get("components", []):
        name = entry.get("component")
        if name in live_by_name and isinstance(entry.get("component_check"), dict):
            entry = _recheck_entry(entry, *live_by_name[name])
            changed.add(name)
        components.append(entry)
    blocking = [b for b in summary.get("blocking", []) if b not in changed]
    blocking += [
        e["component"] for e in components
        if e["component"] in changed and _check.is_refusing(e.get("result")) and (e.get("active") or e.get("persistent"))
    ]
    dev_components = [
        e["component"] for e in components
        if (e.get("component_check") or {}).get("result") == _check.DEV_WARNING and (e.get("active") or e.get("persistent"))
    ]
    return dict(summary, components=components, blocking=blocking, dev_components=dev_components, ok=not blocking)


def fresh_summary() -> dict[str, Any]:
    """Like :func:`local_summary`, but with every version read again from what is installed.

    Status files are written once when a component starts, so after an update they still
    name the old version. The server's own check is re-run and the other components'
    version checks are redone against their installed versions.
    """
    refresh_server_start()
    return refresh_components(local_summary())


def current_refusal() -> Optional[str]:
    """Refusal text for a run. A refusal is re-verified against freshly queried
    versions before it stands, so a stale status file never denies a run."""
    refusal = refusal_message(local_summary())
    if refusal:
        refusal = refusal_message(fresh_summary())
    return refusal


def refusal_message(summary: dict[str, Any]) -> Optional[str]:
    """Text for rejecting a run, or None when it may start."""
    if _check is None or not summary.get("available", True) or summary.get("ok", True):
        return None
    return _check.refusal_message(summary)


# ── Display (CLI) ────────────────────────────────────────────────────────

_RESULT_STYLE = {
    "ok": "green",
    "skipped": "dim",
    "dev-warnung": "yellow",
    "mismatch": "red",
    "version-mismatch": "red",
    "bundle-fehler": "red",
}


def dev_mode_line(summary: dict[str, Any]) -> Optional[str]:
    if not summary.get("dev_mode"):
        return None
    line = "Transport contract check is in DEV mode: mismatches between components are not enforced."
    if summary.get("hashes_differ"):
        line += " The running components currently use different transports."
    return line


def dev_components_line(summary: dict[str, Any]) -> Optional[str]:
    """Which components skip their version check because the bundle says `dev` for them."""
    names = summary.get("dev_components") or []
    if not names:
        return None
    return f"Version check omitted for {', '.join(names)}: the bundle marks them as 'dev'."


def dev_warnings(summary: dict[str, Any]) -> list[str]:
    """Every dev-mode warning line for `connect` and `run` (contract dev mode and dev components)."""
    return [line for line in (dev_mode_line(summary), dev_components_line(summary)) if line]


def render_summary(console, summary: dict[str, Any]) -> bool:
    """Print the contract table for ``raccoon doctor``. Returns True when everything is fine."""
    from rich.table import Table

    console.print()
    console.print("[bold]Transport contract[/bold]")
    if not summary.get("available", True):
        console.print(f"[dim]{summary.get('note', 'not available')}[/dim]")
        return True
    if summary.get("on_device") is False:
        console.print("[dim]This server is not running on a Raccoon device; nothing to enforce.[/dim]")
        return True
    bundle = summary.get("bundle") or {}
    if bundle.get("error"):
        console.print(f"[red]Bundle file problem: {bundle['error']} ({bundle.get('file')})[/red]")
    else:
        expected = summary.get("expected_hash")
        version = bundle.get("transport_version") or "unknown"
        console.print(f"Bundle {bundle.get('name') or 'unknown'} expects {expected} (transport {version})")
    for line in dev_warnings(summary):
        console.print(f"[bold yellow]{line}[/bold yellow]")

    components = summary.get("components") or []
    if not components:
        console.print("[dim]No component has reported yet.[/dim]")
    else:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Component")
        table.add_column("Result")
        table.add_column("State")
        table.add_column("Contract")
        for entry in components:
            result = entry.get("result", "?")
            style = _RESULT_STYLE.get(result, "white")
            state = "running" if entry.get("active") else "not active"
            table.add_row(
                str(entry.get("component", "?")),
                f"[{style}]{result}[/{style}]",
                state,
                str(entry.get("own_hash") or "-"),
            )
        console.print(table)
        for entry in components:
            if entry.get("component") in (summary.get("blocking") or []):
                console.print(f"[red]{entry.get('component')}: {entry.get('message', entry.get('result'))}[/red]")
    ok = bool(summary.get("ok", True))
    if not ok:
        console.print("[red]Runs are refused until this is fixed. Update or reflash so all components match the bundle.[/red]")
    return ok


def fetch_remote_summary(address: str, port: int, api_token: Optional[str]) -> Optional[dict[str, Any]]:
    """Ask the Pi server. None when it cannot be reached or is too old to know the endpoint."""
    import asyncio

    from raccoon_cli.client.api import create_api_client

    async def _fetch():
        async with create_api_client(address, port, api_token) as client:
            return await client.get_contract()

    try:
        return asyncio.run(_fetch())
    except Exception:
        return None
