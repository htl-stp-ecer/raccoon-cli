"""Shared helpers for mission file scaffolding and main.py import management."""

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


def add_mission_import_to_main(project_root: Path, mission_snake: str, mission_pascal: str) -> None:
    """Add mission import and registration to main.py."""
    main_py = project_root / "src" / "main.py"

    if not main_py.exists():
        logger.warning(f"main.py not found at {main_py}")
        return

    content = main_py.read_text(encoding='utf-8')

    import_line = f"from .missions.{mission_snake}_mission import {mission_pascal}Mission"

    if import_line not in content:
        lines = content.split('\n')
        insert_idx = 0

        for i, line in enumerate(lines):
            if 'from .missions.' in line and 'import' in line:
                insert_idx = i + 1

        if insert_idx == 0:
            for i, line in enumerate(lines):
                if line.strip() and not line.startswith('#') and not line.startswith('"""') and 'import' not in line:
                    insert_idx = i
                    break

        lines.insert(insert_idx, import_line)
        content = '\n'.join(lines)
        main_py.write_text(content, encoding='utf-8')


def remove_mission_import_from_main(project_root: Path, mission_snake: str, mission_pascal: str) -> None:
    """Remove mission import from main.py."""
    main_py = project_root / "src" / "main.py"

    if not main_py.exists():
        return

    content = main_py.read_text(encoding='utf-8')
    import_line = f"from .missions.{mission_snake}_mission import {mission_pascal}Mission"

    lines = content.split('\n')
    lines = [line for line in lines if import_line not in line]

    main_py.write_text('\n'.join(lines), encoding='utf-8')
