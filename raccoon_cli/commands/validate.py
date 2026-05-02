"""Validate command for raccoon CLI."""

from __future__ import annotations

import logging

import click
from rich.console import Console

from raccoon_cli.project import ProjectError, require_project, load_project_config
from raccoon_cli.validation import run_validation_or_exit

logger = logging.getLogger("raccoon")


@click.command(name="validate")
@click.option(
    "--no-python-compile",
    is_flag=True,
    help="Skip Python bytecode compile checks for project source files.",
)
@click.option(
    "--no-codegen-probe",
    is_flag=True,
    help="Skip temporary codegen probe validation.",
)
@click.pass_context
def validate_command(
    ctx: click.Context,
    no_python_compile: bool,
    no_codegen_probe: bool,
) -> None:
    """Validate project config and Python source integrity."""
    console: Console = ctx.obj["console"]

    try:
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            raise ProjectError("raccoon.project.yml must be a mapping")

        run_validation_or_exit(
            console,
            project_root,
            config=config,
            python_compile=not no_python_compile,
            codegen_probe=not no_codegen_probe,
        )
        console.print("[green]Validation complete[/green]")

    except ProjectError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unexpected error during validation")
        raise SystemExit(1) from None