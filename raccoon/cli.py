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
@click.option('-o', '--out', type=click.Path(), default='defs.py', help='Output file path')
@click.option('--class-name', default='Defs', help='Name of the generated class')
@click.option('--no-format', is_flag=True, help='Skip black formatting')
def codegen(out, class_name, no_format):
    """Generate Python definitions from raccoon.project.yml."""
    from pathlib import Path

    from raccoon.project import require_project, load_project_config
    from raccoon.codegen import generate_defs_source

    try:
        # Ensure we're in a project directory
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        # Load project config
        logger.info(f"Reading config from raccoon.project.yml")
        data = load_project_config(project_root)
        if not isinstance(data, dict):
            click.echo("Error: raccoon.project.yml must be a mapping", err=True)
            sys.exit(1)

        # Generate source
        logger.info(f"Generating source code...")
        src = generate_defs_source(data, class_name=class_name)

        # Format with black if available
        if not no_format:
            try:
                import black
                logger.info(f"Formatting with black...")
                src = black.format_str(src, mode=black.Mode(line_length=88))
            except ImportError:
                logger.warning("black not installed, skipping formatting")

        # Write output
        out_file = Path(out)
        out_file.write_text(src, encoding="utf-8")
        logger.info(f"Wrote {out_file}")
        click.echo(f"✓ Generated {out_file}")

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
    from pathlib import Path
    from raccoon.project import require_project, load_project_config
    from raccoon.codegen import generate_defs_source
    import subprocess

    try:
        # Ensure we're in a project directory
        project_root = require_project()
        logger.info(f"Running in project: {project_root}")

        # Load project config
        logger.info(f"Reading config from raccoon.project.yml")
        config = load_project_config(project_root)
        if not isinstance(config, dict):
            click.echo("Error: raccoon.project.yml must be a mapping", err=True)
            sys.exit(1)

        # Run codegen
        logger.info(f"Generating source code...")
        src = generate_defs_source(config, class_name='Defs')

        # Format with black if available
        try:
            import black
            logger.info(f"Formatting with black...")
            src = black.format_str(src, mode=black.Mode(line_length=88))
        except ImportError:
            logger.warning("black not installed, skipping formatting")

        # Write output to default location (src/hardware/defs.py)
        out_file = project_root / 'src' / 'hardware' / 'defs.py'
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(src, encoding="utf-8")
        logger.info(f"Wrote {out_file}")

        # Run src.main
        logger.info(f"Running src.main...")
        cmd_parts = [sys.executable, '-m', 'src.main']
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
