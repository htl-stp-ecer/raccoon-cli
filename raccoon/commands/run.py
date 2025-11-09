"""Run command for raccoon CLI."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from raccoon.codegen import create_pipeline
from raccoon.project import ProjectError, load_project_config, require_project

logger = logging.getLogger("raccoon")


@click.command(name="run")
@click.argument("args", nargs=-1)
@click.pass_context
def run_command(ctx: click.Context, args) -> None:
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
