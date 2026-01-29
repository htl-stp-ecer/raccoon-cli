"""Custom setup to build web IDE during pip install."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWebIDE(build_py):
    """Custom build command that builds the web IDE first."""

    def run(self):
        # Skip web IDE build on Pi (server-only install)
        if os.environ.get("RACCOON_SERVER_ONLY"):
            print("RACCOON_SERVER_ONLY set, skipping web IDE build")
            super().run()
            return

        root_dir = Path(__file__).parent
        web_ide_dir = root_dir / "web-ide"
        dist_src = web_ide_dir / "dist" / "WebIDE" / "browser"
        dist_dest = root_dir / "raccoon" / "web-ide-dist"

        # Check if web-ide directory exists
        if not web_ide_dir.exists():
            print("Warning: web-ide directory not found, skipping web IDE build")
            super().run()
            return

        # Check for npm
        npm_cmd = shutil.which("npm")
        if not npm_cmd:
            print("Warning: npm not found, skipping web IDE build")
            print("Install Node.js to enable web IDE support")
            super().run()
            return

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
                print(f"Warning: npm install failed: {result.stderr}")
                super().run()
                return

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
            print(f"Warning: ng build failed: {result.stderr}")
            if result.stdout:
                print(result.stdout)
            super().run()
            return

        # Copy built files to package
        if dist_src.exists():
            print(f"Copying web IDE files to {dist_dest}")
            if dist_dest.exists():
                shutil.rmtree(dist_dest)
            shutil.copytree(dist_src, dist_dest)
        else:
            print(f"Warning: Build output not found at {dist_src}")

        # Continue with normal build
        super().run()


setup(
    cmdclass={
        "build_py": BuildWebIDE,
    },
)
