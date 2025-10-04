"""Main CLI entry point for raccoon."""

import sys
import logging
import click

from raccoon.project import ProjectError

# Configure logging with rich handler for formatted output
try:
    from rich.logging import RichHandler
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=False, show_path=False)]
    )
except ImportError:
    # Fallback to standard logging if rich is not available
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s"
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
@click.argument('script', required=False)
@click.argument('args', nargs=-1)
def run(script, args):
    """Run a project script or command."""
    from raccoon.project import require_project, load_project_config
    import subprocess

    try:
        # Ensure we're in a project directory
        project_root = require_project()
        config = load_project_config(project_root)

        logger.info(f"Running in project: {project_root}")

        # Get scripts from config
        scripts = config.get('scripts', {})

        if script is None:
            # List available scripts
            if not scripts:
                click.echo("No scripts defined in project.yaml")
            else:
                click.echo("Available scripts:")
                for name in scripts:
                    click.echo(f"  - {name}")
            return

        if script not in scripts:
            click.echo(f"Error: Script '{script}' not found in raccoon.project.yml", err=True)
            click.echo("\nAvailable scripts:")
            for name in scripts:
                click.echo(f"  - {name}")
            sys.exit(1)

        # Get the command
        command = scripts[script]

        # If it's a string, split it; if it's a list, use it directly
        if isinstance(command, str):
            import shlex
            cmd_parts = shlex.split(command)
        elif isinstance(command, list):
            cmd_parts = command
        else:
            logger.error(f"Invalid script format for '{script}': must be string or list")
            sys.exit(1)

        # Append any additional arguments
        cmd_parts.extend(args)

        logger.info(f"Executing: {' '.join(cmd_parts)}")

        # Run the command
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
