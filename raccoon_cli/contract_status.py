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


def record_server_start(version: str):
    """Run and record the server's own startup check. Never raises, never stops the server.

    The server must stay up even on a mismatch: it is the one that reports the
    problem. It refuses runs instead (see :func:`refusal_message`).
    """
    if _check is None:
        return None
    try:
        return _check.startup_check(
            SERVER_COMPONENT, version, persistent=True, enforce=False, bundle_key=SERVER_BUNDLE_KEY
        )
    except Exception as exc:  # a broken check must not take the server down
        print(f"Transport contract check failed to run: {exc}")
        return None


def local_summary() -> dict[str, Any]:
    """What ``GET /api/v1/contract`` returns (read from this Pi's status files)."""
    if _check is None:
        return unavailable_summary()
    return {"available": True, **_check.aggregate()}


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
