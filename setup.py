"""Custom setup to build web IDE and generate LCM types during pip install.

Environment variables:
    RACCOON_SKIP_WEBIDE: Set to skip web-ide build entirely (e.g., for server-only installs on Pi)
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


ROOT_DIR = Path(__file__).parent


class WebIDEBuildError(Exception):
    """Raised when web-ide build fails."""

    pass


def generate_lcm_types():
    """Generate Python LCM types from .lcm definitions into raccoon/exlcm/."""
    types_dir = ROOT_DIR / "lcm-messages" / "types"
    target = ROOT_DIR / "raccoon" / "exlcm"

    if not types_dir.exists():
        raise RuntimeError(
            "lcm-messages submodule not initialized. "
            "Run: git submodule update --init"
        )

    lcm_files = sorted(types_dir.glob("*.lcm"))
    if not lcm_files:
        raise RuntimeError("No .lcm files found in lcm-messages/types/")

    # Try lcm-gen directly, fall back to pre-generated files in submodule
    try:
        subprocess.check_call(
            ["lcm-gen", "--python", "--ppath", str(ROOT_DIR / "lcm-messages")]
            + [str(f) for f in lcm_files]
        )
    except FileNotFoundError:
        # lcm-gen not installed — use pre-generated files from submodule
        src = ROOT_DIR / "lcm-messages" / "exlcm"
        if not src.exists() or not any(src.glob("*.py")):
            raise RuntimeError(
                "lcm-gen is not installed and lcm-messages/exlcm/ has no "
                "pre-generated files. Install lcm-gen or run "
                "generate-python-files.sh in the submodule first."
            )

    # Copy generated files into raccoon/exlcm/
    src = ROOT_DIR / "lcm-messages" / "exlcm"
    target.mkdir(parents=True, exist_ok=True)

    # Remove stale files that no longer exist in source
    for old in target.glob("*.py"):
        if not (src / old.name).exists():
            old.unlink()

    for py_file in src.glob("*.py"):
        shutil.copy2(py_file, target / py_file.name)

    # lcm-gen emits `import exlcm` for cross-type references, but the
    # package lives at raccoon.exlcm so we rewrite the imports.
    for py_file in target.glob("*.py"):
        text = py_file.read_text()
        patched = re.sub(
            r"^import exlcm$",
            "from raccoon import exlcm",
            text,
            flags=re.MULTILINE,
        )
        if patched != text:
            py_file.write_text(patched)

    print(f"LCM types: copied {len(list(src.glob('*.py')))} files to {target}")


class BuildWithExtras(build_py):
    """Custom build command that generates LCM types and builds the web IDE."""

    def run(self):
        # Always generate LCM types
        generate_lcm_types()

        # Skip web IDE build if explicitly requested (e.g., server-only install on Pi)
        if os.environ.get("RACCOON_SKIP_WEBIDE"):
            print("RACCOON_SKIP_WEBIDE set, skipping web IDE build")
            super().run()
            return

        root_dir = ROOT_DIR
        web_ide_dir = root_dir / "web-ide"
        dist_src = web_ide_dir / "dist" / "WebIDE" / "browser"
        dist_dest = root_dir / "raccoon" / "web-ide-dist"

        # Check if web-ide directory exists
        if not web_ide_dir.exists():
            raise WebIDEBuildError(
                f"web-ide directory not found at {web_ide_dir}\n"
                "Set RACCOON_SKIP_WEBIDE=1 to skip web-ide build."
            )

        # Check for npm
        npm_cmd = shutil.which("npm")
        if not npm_cmd:
            raise WebIDEBuildError(
                "npm not found. Install Node.js to build web-ide.\n"
                "Set RACCOON_SKIP_WEBIDE=1 to skip web-ide build."
            )

        # Install npm dependencies if needed
        node_modules = web_ide_dir / "node_modules"
        if not node_modules.exists():
            print("Installing web IDE dependencies...")
            result = subprocess.run(
                [npm_cmd, "install"],
                cwd=web_ide_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise WebIDEBuildError(
                    f"npm install failed:\n{result.stderr}\n"
                    "Set RACCOON_SKIP_WEBIDE=1 to skip web-ide build."
                )

        # Build Angular app
        print("Building web IDE...")
        npx_cmd = shutil.which("npx")
        result = subprocess.run(
            [npx_cmd, "ng", "build", "--configuration=production"],
            cwd=web_ide_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error_output = result.stderr
            if result.stdout:
                error_output += f"\n{result.stdout}"
            raise WebIDEBuildError(
                f"ng build failed:\n{error_output}\n"
                "Set RACCOON_SKIP_WEBIDE=1 to skip web-ide build."
            )

        # Copy built files to package
        if dist_src.exists():
            print(f"Copying web IDE files to {dist_dest}")
            if dist_dest.exists():
                shutil.rmtree(dist_dest)
            shutil.copytree(dist_src, dist_dest)
        else:
            raise WebIDEBuildError(
                f"Build output not found at {dist_src}\n"
                "The Angular build may have failed silently.\n"
                "Set RACCOON_SKIP_WEBIDE=1 to skip web-ide build."
            )

        # Continue with normal build
        super().run()


setup(
    cmdclass={
        "build_py": BuildWithExtras,
    },
)
