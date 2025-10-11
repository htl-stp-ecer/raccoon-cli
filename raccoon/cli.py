"""Main CLI entry point for raccoon."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from raccoon.codegen import create_pipeline
from raccoon.logging_utils import configure_logging, render_banner, render_summary
from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
}


def _setup_context(ctx: click.Context) -> None:
    """Ensure console and logging are ready for a command invocation."""
    ctx.ensure_object(dict)

    if not ctx.obj.get("initialized"):
        console = Console()
        summary = configure_logging(console)
        ctx.obj["console"] = console
        ctx.obj["log_summary"] = summary
        ctx.obj["initialized"] = True
        render_banner(console)
    else:
        summary = ctx.obj["log_summary"]

    summary.clear()
    ctx.obj["summary_printed"] = False


def _print_summary(ctx: click.Context) -> None:
    """Render the warning/error summary exactly once per command."""
    if ctx.obj.get("summary_printed"):
        return

    console: Console | None = ctx.obj.get("console")
    summary = ctx.obj.get("log_summary")
    if console is None or summary is None:
        return

    render_summary(console, summary)
    summary.clear()
    ctx.obj["summary_printed"] = True


def _render_codegen_success(
    console: Console,
    results: Dict[str, Path],
    project_root: Path,
    output_dir: Path,
    formatted: bool,
    filtered: bool,
) -> None:
    """Display a concise overlay of generated files."""
    heading = Text(
        "Code generation complete",
        style="bold green",
    )

    subtitle_parts = []
    try:
        output_display = output_dir.relative_to(project_root)
    except ValueError:
        output_display = output_dir
    subtitle_parts.append(f"Output: {output_display}")
    subtitle_parts.append("Formatting: on" if formatted else "Formatting: off")
    if filtered:
        subtitle_parts.append("Mode: filtered")

    subtitle = Text(" | ".join(subtitle_parts), style="dim")

    table = Table(
        "Generator",
        "File",
        box=box.MINIMAL_DOUBLE_HEAD,
        header_style="bold cyan",
        expand=True,
    )

    for name, path in results.items():
        try:
            display_path = path.relative_to(project_root)
        except ValueError:
            display_path = path
        table.add_row(name, str(display_path))

    console.print(
        Panel(
            table,
            title=heading,
            subtitle=subtitle,
            border_style="cyan",
            padding=(1, 2),
        )
    )


@click.group(context_settings=CONTEXT_SETTINGS, no_args_is_help=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Raccoon - Toolchain CLI for libstp projects."""
    _setup_context(ctx)


@main.command()
@click.option(
    "--only",
    multiple=True,
    help="Generate specific file(s): defs, robot. May be given multiple times.",
)
@click.option("--no-format", is_flag=True, help="Skip black formatting")
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(),
    default=None,
    help="Override output directory (default: src/hardware/)",
)
@click.pass_context
def codegen(ctx: click.Context, only, no_format: bool, output_dir: str | None) -> None:
    """Generate Python code from raccoon.project.yml."""
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

        if output_dir:
            out_dir = Path(output_dir)
        else:
            out_dir = project_root / "src" / "hardware"

        pipeline = create_pipeline()

        format_code = not no_format
        filtered = bool(only)
        if filtered:
            results = pipeline.run_specific(list(only), config, out_dir, format_code)
        else:
            results = pipeline.run_all(config, out_dir, format_code)

        _render_codegen_success(
            console,
            results,
            project_root,
            out_dir,
            formatted=format_code,
            filtered=filtered,
        )
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except Exception:
        logger.exception("Unexpected error during code generation")
        raise SystemExit(1) from None
    finally:
        _print_summary(ctx)


@main.command()
@click.argument("args", nargs=-1)
@click.pass_context
def run(ctx: click.Context, args) -> None:
    """Run codegen and then execute src.main."""
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

        pipeline = create_pipeline()
        output_dir = project_root / "src" / "hardware"
        pipeline.run_all(config, output_dir, format_code=True)

        logger.info("Running src.main...")
        cmd_parts = [sys.executable, "-m", "src.main", *args]
        logger.info(f"Executing: {' '.join(cmd_parts)}")

        result = subprocess.run(cmd_parts, cwd=project_root)

        exit_style = "bold green" if result.returncode == 0 else "bold red"
        console.print(
            Panel.fit(
                Text(f"src.main exited with code {result.returncode}", style=exit_style),
                border_style="green" if result.returncode == 0 else "red",
            )
        )

        if result.returncode != 0:
            raise SystemExit(result.returncode)
    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except Exception:
        logger.exception("Unexpected error while running project")
        raise SystemExit(1) from None
    finally:
        _print_summary(ctx)


if __name__ == "__main__":
    main()
