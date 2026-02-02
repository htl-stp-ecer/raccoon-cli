"""Web command - serve the web IDE with full backend support."""

import threading
import webbrowser
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel


def get_web_ide_dist_path() -> Path:
    """Get the path to the bundled web IDE static files."""
    return Path(__file__).parent.parent / "web-ide-dist"


@click.command(name="web")
@click.option("--port", "-p", type=int, default=4200, help="Port to serve on")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
@click.pass_context
def web_command(ctx: click.Context, port: int, no_open: bool) -> None:
    """Start the Web IDE with full backend support.

    Serves the web IDE with a full API backend on a local port.
    The project root is the current working directory.

    Projects will be stored in and loaded from the current directory.

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

    # Create the FastAPI app with current directory as project root
    from raccoon.ide.app import create_app

    project_root = Path.cwd()
    app = create_app(project_root=project_root)

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
