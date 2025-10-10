"""Main CLI entry point for raccoon."""

import logging
import sys

import click
from rich.logging import RichHandler

from raccoon.project import ProjectError

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_time=False, show_path=False)]
)

logger = logging.getLogger("raccoon")


@click.group()
def main():
    """Raccoon - Toolchain CLI for libstp projects."""
    pass


@main.command()
@click.option(
    '--only',
    multiple=True,
    help='Generate only specific file(s): defs, robot. Can be specified multiple times.',
)
@click.option('--no-format', is_flag=True, help='Skip black formatting')
@click.option(
    '-o',
    '--output-dir',
    type=click.Path(),
    default=None,
    help='Output directory (default: src/hardware/)',
)
def codegen(only, no_format, output_dir):
    """Generate Python code from raccoon.project.yml."""
    from pathlib import Path

    from raccoon.project import require_project, load_project_config
    from raccoon.codegen import create_pipeline

    try:
        # Ensure we're in a project directory
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        # Add project root to sys.path so custom modules can be imported
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        # Load project config
        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            click.echo("Error: raccoon.project.yml must be a mapping", err=True)
            sys.exit(1)

        # Determine output directory
        if output_dir:
            out_dir = Path(output_dir)
        else:
            out_dir = project_root / "src" / "hardware"

        # Create pipeline
        pipeline = create_pipeline()

        # Generate code
        format_code = not no_format
        if only:
            # Generate specific files
            results = pipeline.run_specific(list(only), config, out_dir, format_code)
        else:
            # Generate all files
            results = pipeline.run_all(config, out_dir, format_code)

        # Print summary
        click.echo(f"✓ Generated {len(results)} file(s) in {out_dir}")
        for name, path in results.items():
            click.echo(f"  - {path.name}")

    except ProjectError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument('args', nargs=-1)
def run(args):
    """Run codegen and then execute src.main."""
    from raccoon.project import require_project, load_project_config
    from raccoon.codegen import create_pipeline
    import subprocess

    try:
        # Ensure we're in a project directory
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        # Add project root to sys.path so custom modules can be imported
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        # Load project config
        logger.info("Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            click.echo("Error: raccoon.project.yml must be a mapping", err=True)
            sys.exit(1)

        # Create pipeline and run codegen
        pipeline = create_pipeline()
        output_dir = project_root / "src" / "hardware"

        # Generate all files
        pipeline.run_all(config, output_dir, format_code=True)

        # Run src.main
        logger.info("Running src.main...")
        cmd_parts = [sys.executable, "-m", "src.main"]
        cmd_parts.extend(args)

        logger.info(f"Executing: {' '.join(cmd_parts)}")
        result = subprocess.run(cmd_parts, cwd=project_root)
        sys.exit(result.returncode)

    except ProjectError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
