"""Web command - serve the web IDE with full backend support."""

import threading
import webbrowser
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel


def get_web_ide_dist_path() -> Path:
    """Get the path to the bundled web IDE static files."""
    return Path(__file__).parent.parent / "web-ide-dist"


def _detect_project(cwd: Path) -> tuple[Path, str | None]:
    """Detect if cwd is inside a project and return (project_root, project_uuid).

    If raccoon.project.yml is found in cwd (or a parent), sets project_root
    to the parent of the project directory so the backend can discover it,
    and returns the project UUID for direct navigation.

    Returns:
        (project_root, project_uuid) - project_uuid is None if not in a project.
    """
    project_file = cwd / "raccoon.project.yml"
    if not project_file.exists():
        return cwd, None

    try:
        from raccoon_cli.yaml_utils import load_yaml
        config = load_yaml(project_file)
        if isinstance(config, dict) and config.get("uuid"):
            return cwd.parent, str(config["uuid"])
    except Exception:
        pass

    return cwd, None


@click.command(name="web")
@click.option("--port", "-p", type=int, default=4200, help="Port to serve on")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
@click.pass_context
def web_command(ctx: click.Context, port: int, no_open: bool) -> None:
    """Start the Web IDE with full backend support.

    Serves the web IDE with a full API backend on a local port.

    When run inside a project directory (containing raccoon.project.yml),
    the browser opens directly to that project.

    Automatically opens the browser unless --no-open is specified.

    Examples:

        raccoon web              # Serve on port 4200

        raccoon web -p 8080      # Serve on port 8080

        raccoon web --no-open    # Don't auto-open browser
    """
    console: Console = ctx.obj.get("console", Console())

    dist_path = get_web_ide_dist_path()

    # Verify dist exists
    if not dist_path.exists():
        console.print(
            Panel(
                "[red]Web IDE files not found.[/red]\n\n"
                "The web IDE was not built during installation.\n"
                "Ensure Node.js is installed and reinstall with:\n"
                "  [cyan]pip install .[/cyan]",
                title="Error",
            )
        )
        raise SystemExit(1)

    # Import uvicorn and create app
    try:
        import uvicorn
    except ImportError:
        console.print(
            Panel(
                "[red]uvicorn is not installed.[/red]\n\n"
                "Install it with:\n"
                "  [cyan]pip install uvicorn[standard][/cyan]",
                title="Error",
            )
        )
        raise SystemExit(1)

    # Detect if we're inside a project directory
    project_root, project_uuid = _detect_project(Path.cwd())

    # Create the FastAPI app
    from raccoon_cli.ide.app import create_app

    app = create_app(project_root=project_root)

    # Build URL - navigate directly to project if detected
    if project_uuid:
        url = f"http://localhost:{port}/WebIDE/projects/{project_uuid}"
    else:
        url = f"http://localhost:{port}/WebIDE/"

    console.print(
        Panel(
            f"[green]Web IDE is running at:[/green]\n\n"
            f"  [cyan bold]{url}[/cyan bold]\n\n"
            f"[dim]Project root: {project_root}[/dim]\n"
            f"[dim]Press Ctrl+C to stop[/dim]",
            title="Raccoon Web IDE",
        )
    )

    # Open browser if not disabled
    if not no_open:
        def open_browser():
            import time
            time.sleep(0.5)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    # Run the server
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=True,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down server...[/yellow]")
    except OSError as e:
        if "Address already in use" in str(e) or "address already in use" in str(e).lower():
            console.print(
                f"[red]Port {port} is already in use. Try a different port with -p.[/red]"
            )
        else:
            console.print(f"[red]Failed to start server: {e}[/red]")
        raise SystemExit(1)
