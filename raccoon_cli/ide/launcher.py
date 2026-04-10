"""PyCharm launcher for different platforms."""

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional


class PyCharmLauncher:
    """
    Launch PyCharm with a project.

    Supports Windows, macOS, and Linux.
    Attempts to find PyCharm in common installation locations.
    """

    # Common PyCharm installation paths
    WINDOWS_PATHS = [
        # JetBrains Toolbox
        Path.home() / "AppData/Local/JetBrains/Toolbox/scripts/pycharm.cmd",
        # Community Edition
        Path("C:/Program Files/JetBrains/PyCharm Community Edition 2024.1/bin/pycharm64.exe"),
        Path("C:/Program Files/JetBrains/PyCharm Community Edition 2023.3/bin/pycharm64.exe"),
        Path("C:/Program Files/JetBrains/PyCharm Community Edition 2023.2/bin/pycharm64.exe"),
        # Professional Edition
        Path("C:/Program Files/JetBrains/PyCharm 2024.1/bin/pycharm64.exe"),
        Path("C:/Program Files/JetBrains/PyCharm 2023.3/bin/pycharm64.exe"),
        Path("C:/Program Files/JetBrains/PyCharm 2023.2/bin/pycharm64.exe"),
    ]

    LINUX_PATHS = [
        # JetBrains Toolbox
        Path.home() / ".local/share/JetBrains/Toolbox/scripts/pycharm",
        # Snap installation
        Path("/snap/bin/pycharm-community"),
        Path("/snap/bin/pycharm-professional"),
        # Flatpak
        Path("/var/lib/flatpak/exports/bin/com.jetbrains.PyCharm-Community"),
        Path("/var/lib/flatpak/exports/bin/com.jetbrains.PyCharm-Professional"),
        # Manual installation
        Path.home() / ".local/bin/pycharm",
        Path("/opt/pycharm/bin/pycharm.sh"),
        Path("/opt/pycharm-community/bin/pycharm.sh"),
    ]

    def __init__(self):
        self.system = platform.system()

    def find_pycharm(self) -> Optional[Path]:
        """
        Find PyCharm installation on the system.

        Returns:
            Path to PyCharm executable or None if not found
        """
        if self.system == "Windows":
            # Check known paths
            for path in self.WINDOWS_PATHS:
                if path.exists():
                    return path
            # Try PATH
            pycharm = shutil.which("pycharm")
            if pycharm:
                return Path(pycharm)
            return None

        elif self.system == "Darwin":  # macOS
            # Try common app locations
            app_paths = [
                Path("/Applications/PyCharm.app"),
                Path("/Applications/PyCharm CE.app"),
                Path("/Applications/PyCharm Community Edition.app"),
                Path.home() / "Applications/PyCharm.app",
                Path.home() / "Applications/PyCharm CE.app",
            ]
            for app_path in app_paths:
                if app_path.exists():
                    return app_path
            return None

        elif self.system == "Linux":
            # Check known paths
            for path in self.LINUX_PATHS:
                if path.exists():
                    return path
            # Try PATH
            pycharm = shutil.which("pycharm")
            if pycharm:
                return Path(pycharm)
            pycharm = shutil.which("pycharm-community")
            if pycharm:
                return Path(pycharm)
            return None

        return None

    def launch(self, project_path: Path) -> bool:
        """
        Launch PyCharm with the given project.

        Args:
            project_path: Path to the project directory

        Returns:
            True if launched successfully, False otherwise
        """
        pycharm_path = self.find_pycharm()

        if pycharm_path is None:
            return False

        try:
            if self.system == "Windows":
                # Use shell=True for .cmd files
                if pycharm_path.suffix == ".cmd":
                    subprocess.Popen(
                        f'"{pycharm_path}" "{project_path}"',
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    subprocess.Popen(
                        [str(pycharm_path), str(project_path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

            elif self.system == "Darwin":
                # macOS: use 'open -a' for .app bundles
                subprocess.Popen(
                    ["open", "-a", str(pycharm_path), str(project_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            elif self.system == "Linux":
                subprocess.Popen(
                    [str(pycharm_path), str(project_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            return True

        except Exception:
            return False

    def is_available(self) -> bool:
        """Check if PyCharm is installed and available."""
        return self.find_pycharm() is not None
