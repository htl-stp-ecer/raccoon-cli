"""Shared helpers for mission file scaffolding."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader

from raccoon_cli.project import ProjectError

logger = logging.getLogger("raccoon")


def get_templates_dir() -> Path:
    """Get the templates directory path."""
    templates_dir = Path(__file__).parent / "templates"
    if templates_dir.exists():
        return templates_dir

    raise ProjectError(
        f"Templates directory not found at {templates_dir}.\n"
        f"Please reinstall the raccoon package."
    )


def render_template(template_path: Path, output_path: Path, context: Dict[str, Any]) -> None:
    """Render a Jinja2 template file to an output path."""
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        extensions=['jinja2_time.TimeExtension']
    )

    template_name = template_path.name
    template = env.get_template(template_name)
    rendered = template.render(**context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding='utf-8')


def copy_template_dir(template_dir: Path, target_dir: Path, context: Dict[str, Any]) -> None:
    """Recursively copy and render a template directory.

    Files ending with ``.jinja`` are rendered as templates.
    Other files are copied as-is.
    Filenames with ``{{...}}`` are also rendered.
    """
    for item in template_dir.rglob('*'):
        if item.is_file():
            if item.name in ['copier.yaml', 'codemods.yaml.jinja']:
                continue

            rel_path = item.relative_to(template_dir)

            output_path_str = str(rel_path)
            for key, value in context.items():
                output_path_str = output_path_str.replace(f"{{{{{key}}}}}", str(value))

            output_path = target_dir / output_path_str

            if item.suffix == '.jinja':
                output_path = output_path.with_suffix('')
                render_template(item, output_path, context)
            else:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, output_path)
