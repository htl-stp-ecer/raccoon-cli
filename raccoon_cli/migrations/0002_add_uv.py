"""Migration 0002: Add pyproject.toml with uv entry point and run uv lock."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

NUMBER = 2
DESCRIPTION = "Add pyproject.toml + uv.lock (uv-based project setup)"


def run(project_root: Path) -> None:
    """Generate pyproject.toml from raccoon.project.yml and run uv lock."""
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        _ensure_entry_point(pyproject)
    else:
        _create_pyproject(project_root, pyproject)

    _ensure_main_function(project_root / "src" / "main.py")
    _run_uv_lock(project_root)


def _create_pyproject(project_root: Path, pyproject: Path) -> None:
    """Generate a minimal pyproject.toml from raccoon.project.yml."""
    project_name = project_root.name.lower().replace(" ", "-")

    raccoon_yml = project_root / "raccoon.project.yml"
    if raccoon_yml.exists():
        for line in raccoon_yml.read_text().splitlines():
            m = re.match(r"^name:\s*(.+)", line)
            if m:
                project_name = m.group(1).strip().lower().replace(" ", "-")
                break

    pyproject.write_text(
        f'[project]\n'
        f'name = "{project_name}"\n'
        f'version = "0.1.0"\n'
        f'requires-python = ">=3.11"\n'
        f'dependencies = [\n'
        f'    "raccoon>=1.0.0",\n'
        f']\n'
        f'\n'
        f'[project.scripts]\n'
        f'start = "src.main:main"\n'
    )


def _ensure_entry_point(pyproject: Path) -> None:
    """Add [project.scripts] start entry if missing."""
    content = pyproject.read_text()
    if "start" in content and "src.main:main" in content:
        return
    if "[project.scripts]" not in content:
        content = content.rstrip() + '\n\n[project.scripts]\nstart = "src.main:main"\n'
    else:
        content = re.sub(
            r"(\[project\.scripts\]\n)",
            r'\1start = "src.main:main"\n',
            content,
        )
    pyproject.write_text(content)


def _ensure_main_function(main_py: Path) -> None:
    """Wrap module-level robot.start() in a main() function if not already done."""
    if not main_py.exists():
        return
    source = main_py.read_text()
    if "def main(" in source:
        return

    # Replace: `robot = Robot()\n...\nif __name__ == "__main__":\n    robot.start()`
    # With the main() wrapper pattern.
    new_source = re.sub(
        r'^(robot\s*=\s*Robot\(\).*?)\nif __name__\s*==\s*["\']__main__["\']\s*:\n\s+robot\.start\(\)',
        lambda m: (
            "\ndef main():\n"
            "    robot = Robot()\n"
            "    robot.start()\n"
            "\n\n"
            'if __name__ == "__main__":\n'
            "    main()"
        ),
        source,
        flags=re.DOTALL,
    )
    if new_source != source:
        main_py.write_text(new_source)


def _run_uv_lock(project_root: Path) -> None:
    """Run uv lock to generate or refresh uv.lock."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError(
            "uv is not installed or not in PATH. "
            "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    subprocess.run([uv, "lock"], cwd=project_root, check=True)
