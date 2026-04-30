"""Codegen command for raccoon CLI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from raccoon_cli.codegen import create_pipeline
from raccoon_cli.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


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


def _resolve_ftmap_paths(config: dict, project_root: Path) -> dict:
    """Replace table_map file-path strings with the parsed .ftmap JSON dict."""
    import json
    import copy

    physical = config.get("robot", {}).get("physical", {})
    table_map = physical.get("table_map")
    if not isinstance(table_map, str):
        return config

    ftmap_path = project_root / table_map
    if not ftmap_path.exists():
        raise ProjectError(f"table_map file not found: {ftmap_path}")

    config = copy.deepcopy(config)
    with open(ftmap_path, encoding="utf-8") as f:
        config["robot"]["physical"]["table_map"] = json.load(f)
    return config


def _codegen_local(
    console: Console,
    project_root: Path,
    config: dict,
    only: tuple,
    no_format: bool,
    output_dir: str | None,
) -> None:
    """Run code generation locally."""
    import sys

    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = project_root / "src" / "hardware"

    config = _resolve_ftmap_paths(config, project_root)

    # Add project root to sys.path so user-defined types (e.g.
    # src.hardware.thresholded_sensor.ThresholdedSensor) can be resolved.
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

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


@click.command(name="codegen")
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
def codegen_command(
    ctx: click.Context,
    only: tuple,
    no_format: bool,
    output_dir: str | None,
) -> None:
    """Generate Python code from raccoon.project.yml.

    Runs locally using the raccoon type index — no Pi connection needed.
    """
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

        _codegen_local(console, project_root, config, only, no_format, output_dir)

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error during code generation")
        raise SystemExit(1) from None
