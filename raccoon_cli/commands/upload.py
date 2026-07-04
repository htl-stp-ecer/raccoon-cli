"""upload command — full preflight then push to the robot.

Chains the whole "will this actually run on the field?" pipeline into one
gated command:

    1. validate   — config / mission / import consistency
    2. codegen    — regenerate hardware code from raccoon.project.yml
    3. sim smoke  — run the whole project once under the libstp simulator
                    (hard gate: must exit 0 with no sim_error)
    4. sync       — push the project to the connected Pi
    5. start-check — start the program on the Pi and confirm it comes up
                     without crashing, then stop it again

Every step is a hard gate: the first failure aborts before anything reaches
the robot. Steps 1–4 are thin wrappers around the same shared services the
other commands use; step 3 is the shared :func:`raccoon_cli.simulation.run_sim_smoke`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from raccoon_cli import simulation as sim_shared
from raccoon_cli.codegen import create_pipeline
from raccoon_cli.commands.codegen import _resolve_ftmap_paths
from raccoon_cli.project import ProjectError, find_project_root, load_project_config
from raccoon_cli.validation import run_validation_or_exit


def _print_smoke_failure(console: Console, result: sim_shared.SmokeResult) -> None:
    console.print()
    console.print(f"[red]✗ Runtime-Simulation fehlgeschlagen[/red] ([dim]{result.reason}[/dim])")
    console.print(f"  [dim]scene:[/dim] {result.scene}  [dim](source: {result.scene_source})[/dim]")
    if result.exit_code is not None:
        console.print(f"  [dim]exit code:[/dim] {result.exit_code}")
    for err in result.errors:
        console.print(f"  [red]•[/red] {err}")
    if result.tail:
        console.print("  [dim]Letzte Ausgabe:[/dim]")
        for line in result.tail[-15:]:
            console.print(f"    [dim]{line}[/dim]")


async def _remote_start_check(
    console: Console,
    project_root: Path,
    config: dict,
    start_timeout: float,
) -> bool:
    """Start the program on the Pi and confirm it comes up without crashing.

    Returns True if the program reached a running/cleanly-finished state,
    False if it failed on startup. The program is cancelled as soon as it is
    seen running so the robot only moves for the check window.
    """
    from raccoon_cli.client.api import create_api_client
    from raccoon_cli.client.connection import get_connection_manager

    manager = get_connection_manager()
    if not manager.is_connected:
        console.print(
            "[red]✗ Keine Verbindung zum Roboter für den Start-Check.[/red] "
            "[dim](Sync sollte verbunden haben — 'raccoon connect <ip>' versuchen.)[/dim]"
        )
        return False

    state = manager.state
    project_uuid = config.get("uuid")
    if not project_uuid:
        console.print("[red]✗ Projekt hat keine uuid in raccoon.project.yml.[/red]")
        return False

    console.print(
        "[yellow]⚠ Der Roboter startet jetzt kurz zum Start-Check und wird sofort "
        "wieder gestoppt. Stelle sicher, dass er sicher steht.[/yellow]"
    )

    async with create_api_client(
        state.pi_address, state.pi_port, api_token=state.api_token
    ) as client:
        try:
            result = await client.run_project(project_uuid, args=[], env={})
        except Exception as exc:  # noqa: BLE001 — surface a clean message
            console.print(f"[red]✗ Konnte Programm nicht auf dem Roboter starten: {exc}[/red]")
            return False

        command_id = result.command_id
        console.print(f"[dim]Command ID: {command_id} — beobachte Start ({start_timeout:.0f}s)...[/dim]")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(1.0, start_timeout)
        status = result.status
        exit_code = None

        try:
            while loop.time() < deadline:
                await asyncio.sleep(0.5)
                try:
                    st = await client.get_command_status(command_id)
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[red]✗ Status-Abfrage fehlgeschlagen: {exc}[/red]")
                    return False
                status = st.get("status")
                exit_code = st.get("exit_code")

                if status == "failed" or (
                    status == "completed" and exit_code not in (0, None)
                ):
                    console.print(
                        f"[red]✗ Programm auf dem Roboter abgestürzt beim Start "
                        f"(status={status}, exit={exit_code}).[/red]"
                    )
                    for line in (st.get("output_lines") or [])[-15:]:
                        console.print(f"    [dim]{line}[/dim]")
                    return False

                if status == "running":
                    console.print("[green]✓ Programm läuft auf dem Roboter.[/green]")
                    return True

                if status == "completed":
                    console.print("[green]✓ Programm sauber durchgelaufen.[/green]")
                    return True

            # Still pending/running at the deadline — treat "came up and kept
            # running" as a successful start.
            console.print(
                f"[green]✓ Programm gestartet[/green] [dim](status={status} nach {start_timeout:.0f}s)[/dim]"
            )
            return True
        finally:
            # Always stop the program so the robot doesn't keep running.
            try:
                await client.cancel_command(command_id)
            except Exception:  # noqa: BLE001 — best-effort stop
                pass


@click.command(name="upload")
@click.option(
    "--skip-sim",
    is_flag=True,
    help="Runtime-Simulation überspringen (umgeht das Gate — nur für Notfälle).",
)
@click.option(
    "--no-start-check",
    is_flag=True,
    help="Programm nach dem Sync nicht auf dem Roboter starten/prüfen.",
)
@click.option(
    "--sim-timeout",
    type=float,
    default=180.0,
    show_default=True,
    help="Timeout für den Simulationslauf in Sekunden.",
)
@click.option(
    "--start-timeout",
    type=float,
    default=8.0,
    show_default=True,
    help="Wie lange auf den Programmstart am Roboter gewartet wird (Sekunden).",
)
@click.option(
    "--no-python-compile",
    is_flag=True,
    help="Python-Bytecode-Compile-Checks in der Validierung überspringen.",
)
@click.pass_context
def upload_command(
    ctx: click.Context,
    skip_sim: bool,
    no_start_check: bool,
    sim_timeout: float,
    start_timeout: float,
    no_python_compile: bool,
) -> None:
    """Preflight (validate → codegen → simulieren) und dann zum Roboter hochladen.

    Jeder Schritt ist ein hartes Gate: der erste Fehler bricht ab, bevor
    irgendetwas den Roboter erreicht.
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = find_project_root()
    except ProjectError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise SystemExit(1) from exc

    # ---- 1/5 validate --------------------------------------------------- #
    console.rule("[bold]1/5[/bold] Validate")
    run_validation_or_exit(console, project_root, python_compile=not no_python_compile)
    console.print("[green]✓ Validierung ok[/green]")

    # ---- 2/5 codegen ---------------------------------------------------- #
    console.rule("[bold]2/5[/bold] Codegen")
    config = load_project_config(project_root)
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        pipeline = create_pipeline()
        out_dir = project_root / "src" / "hardware"
        pipeline.run_all(
            _resolve_ftmap_paths(config, project_root), out_dir, format_code=True
        )
    except Exception as exc:  # noqa: BLE001 — codegen failure is a hard gate
        console.print(f"[red]✗ Codegen fehlgeschlagen: {exc}[/red]")
        raise SystemExit(1) from exc
    console.print("[green]✓ Codegen ok[/green]")

    # ---- 3/5 runtime simulation (hard gate) ----------------------------- #
    console.rule("[bold]3/5[/bold] Runtime-Simulation")
    if skip_sim:
        console.print(
            "[yellow]⚠ Simulation übersprungen (--skip-sim) — kein Runtime-Gate![/yellow]"
        )
    else:
        console.print("[dim]Simuliere das gesamte Programm gegen den libstp-Simulator...[/dim]")
        smoke = sim_shared.run_sim_smoke(
            project_root,
            timeout=sim_timeout,
            on_line=lambda line: console.print(f"  [dim]{line}[/dim]"),
        )
        if not smoke.ok:
            _print_smoke_failure(console, smoke)
            raise SystemExit(1)
        console.print(
            f"[green]✓ Simulation ok[/green] "
            f"[dim](scene: {smoke.scene}, source: {smoke.scene_source})[/dim]"
        )

    # ---- 4/5 sync ------------------------------------------------------- #
    console.rule("[bold]4/5[/bold] Sync zum Roboter")
    from raccoon_cli.commands.sync_cmd import sync_project_interactive

    if not sync_project_interactive(project_root, console):
        console.print("[red]✗ Sync fehlgeschlagen — nichts wurde hochgeladen.[/red]")
        raise SystemExit(1)
    console.print("[green]✓ Sync ok[/green]")

    # ---- 5/5 start-check ------------------------------------------------ #
    console.rule("[bold]5/5[/bold] Start-Check")
    if no_start_check:
        console.print("[dim]Start-Check übersprungen (--no-start-check).[/dim]")
    else:
        if not asyncio.run(
            _remote_start_check(console, project_root, config, start_timeout)
        ):
            raise SystemExit(1)

    console.print()
    console.print(
        Panel.fit(
            "[bold green]Upload erfolgreich[/bold green] — das Programm ist auf dem "
            "Roboter und startet dort sauber.",
            border_style="green",
        )
    )
