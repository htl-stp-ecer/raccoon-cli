"""Web command - serve the web IDE."""

import http.server
import os
import socketserver
import threading
import webbrowser
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel


def get_web_ide_dist_path() -> Path:
    """Get the path to the bundled web IDE static files."""
    return Path(__file__).parent.parent / "web-ide-dist"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler that serves files quietly and handles SPA routing."""

    def __init__(self, *args, directory: str, **kwargs):
        self.base_directory = directory
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle GET requests with SPA fallback."""
        # Remove /WebIDE prefix if present (matches Angular baseHref)
        path = self.path
        if path.startswith("/WebIDE"):
            path = path[7:] or "/"

        # Check if file exists
        file_path = Path(self.base_directory) / path.lstrip("/")

        if file_path.exists() and file_path.is_file():
            self.path = path
            return super().do_GET()

        # For non-file paths, serve index.html (SPA routing)
        if not path.split("?")[0].split("#")[0].rsplit(".", 1)[-1] in [
            "js",
            "css",
            "ico",
            "png",
            "jpg",
            "svg",
            "woff",
            "woff2",
            "ttf",
            "map",
            "json",
        ]:
            self.path = "/index.html"

        return super().do_GET()


@click.command(name="web")
@click.option("--port", "-p", type=int, default=4200, help="Port to serve on")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
@click.pass_context
def web_command(ctx: click.Context, port: int, no_open: bool) -> None:
    """Start and serve the web IDE.

    Serves the pre-built web IDE on a local port.
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

    # Start the server
    os.chdir(dist_path)

    handler = lambda *args, **kwargs: QuietHandler(
        *args, directory=str(dist_path), **kwargs
    )

    try:
        with socketserver.TCPServer(("", port), handler) as httpd:
            url = f"http://localhost:{port}/WebIDE/"
            console.print(
                Panel(
                    f"[green]Web IDE is running at:[/green]\n\n"
                    f"  [cyan bold]{url}[/cyan bold]\n\n"
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

            # Serve forever
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                console.print("\n[yellow]Shutting down server...[/yellow]")

    except OSError as e:
        if "Address already in use" in str(e):
            console.print(
                f"[red]Port {port} is already in use. Try a different port with -p.[/red]"
            )
        else:
            console.print(f"[red]Failed to start server: {e}[/red]")
        raise SystemExit(1)
