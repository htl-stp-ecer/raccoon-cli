"""Connection state management for Raccoon client."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from raccoon_cli import __version__ as CLIENT_VERSION


# Minimum required paramiko version for proper SSH protocol support
MIN_PARAMIKO_VERSION = "3.0.0"


class VersionMismatchError(Exception):
    """Raised when client and server versions don't match."""

    def __init__(self, client_version: str, server_version: str):
        self.client_version = client_version
        self.server_version = server_version
        super().__init__(
            f"Version mismatch: client is {client_version}, server is {server_version}. "
            f"Please update {'the server (Pi)' if client_version > server_version else 'your client'} "
            f"to match versions."
        )


class ParamikoVersionError(Exception):
    """Raised when paramiko version is too old."""

    def __init__(self, installed_version: str, min_version: str):
        self.installed_version = installed_version
        self.min_version = min_version
        super().__init__(
            f"paramiko version {installed_version} is too old. "
            f"Minimum required: {min_version}. "
            f"Run: pip install --upgrade 'paramiko>={min_version}'"
        )


def check_paramiko_version() -> None:
    """
    Check that paramiko version meets minimum requirements.

    Raises:
        ParamikoVersionError: If paramiko is too old
    """
    try:
        import paramiko
        from packaging import version
    except ImportError:
        # If packaging isn't available, try basic comparison
        import paramiko
        installed = paramiko.__version__
        # Simple version check for common cases
        parts = installed.split(".")
        min_parts = MIN_PARAMIKO_VERSION.split(".")
        try:
            if int(parts[0]) < int(min_parts[0]):
                raise ParamikoVersionError(installed, MIN_PARAMIKO_VERSION)
        except (ValueError, IndexError):
            pass  # Can't parse, skip check
        return

    installed = paramiko.__version__
    if version.parse(installed) < version.parse(MIN_PARAMIKO_VERSION):
        raise ParamikoVersionError(installed, MIN_PARAMIKO_VERSION)


@dataclass
class ConnectionState:
    """Current connection state to a Raccoon Pi."""

    connected: bool = False
    pi_address: Optional[str] = None
    pi_port: int = 8421
    pi_user: str = "pi"
    pi_hostname: Optional[str] = None
    pi_version: Optional[str] = None
    api_token: Optional[str] = None
    connected_at: Optional[str] = None
    discovery_method: Optional[str] = None


@dataclass
class ConnectionConfig:
    """Connection configuration stored in project config (no sensitive data)."""

    pi_address: Optional[str] = None
    pi_port: int = 8421
    pi_user: str = "pi"
    remote_path: Optional[str] = None
    auto_connect: bool = True

    def to_dict(self) -> dict:
        """Convert to dictionary for YAML serialization."""
        return {
            "pi_address": self.pi_address,
            "pi_port": self.pi_port,
            "pi_user": self.pi_user,
            "remote_path": self.remote_path,
            "auto_connect": self.auto_connect,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionConfig":
        """Create from dictionary."""
        return cls(
            pi_address=data.get("pi_address"),
            pi_port=data.get("pi_port", 8421),
            pi_user=data.get("pi_user", "pi"),
            remote_path=data.get("remote_path"),
            auto_connect=data.get("auto_connect", True),
        )


class ConnectionManager:
    """
    Manages the connection state between the laptop CLI and the Pi.

    Handles:
    - Connection establishment and verification
    - API token retrieval via SSH
    - Configuration persistence
    - SSH client management for SFTP
    """

    GLOBAL_CONFIG_PATH = Path.home() / ".raccoon" / "config.yml"
    TOKEN_FILE_PATH = ".raccoon/api_token"  # Path on Pi

    def __init__(self):
        self._state = ConnectionState()
        self._ssh_client = None  # Will hold paramiko SSHClient

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to a Pi."""
        return self._state.connected

    @property
    def api_token(self) -> Optional[str]:
        """Get the API token for the current connection."""
        return self._state.api_token

    def _fetch_api_token_via_ssh(self, address: str, user: str) -> Optional[str]:
        """
        Fetch the API token from the Pi via SSH.

        The token is stored at ~/.raccoon/api_token on the Pi.
        This requires SSH access (key-based auth recommended).

        Raises:
            ParamikoVersionError: If paramiko version is too old
        """
        # Check paramiko version before any SSH operations
        check_paramiko_version()

        import paramiko

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=address, username=user)

            # Read the token file
            stdin, stdout, stderr = client.exec_command(f"cat ~/{self.TOKEN_FILE_PATH}")
            token = stdout.read().decode().strip()
            error = stderr.read().decode().strip()

            client.close()

            if token:
                return token

            return None

        except ParamikoVersionError:
            raise  # Let version errors propagate
        except Exception:
            return None

    async def connect(
        self,
        address: str,
        port: int = 8421,
        user: str = "pi",
        auto_discover: bool = False,  # Kept for backwards compatibility, ignored
    ) -> bool:
        """
        Connect to a Raccoon Pi server.

        This:
        1. Verifies the HTTP server is reachable
        2. Fetches the API token via SSH
        3. Stores the connection state

        Args:
            address: IP address or hostname
            port: Server port
            user: SSH username for SFTP
            auto_discover: Deprecated, ignored

        Returns:
            True if connection successful

        Raises:
            ParamikoVersionError: If paramiko version is too old
        """
        import httpx

        # Check paramiko version FIRST to catch SSH protocol issues early
        check_paramiko_version()

        # Verify connection to server (health endpoint is public)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"http://{address}:{port}/health")
                if response.status_code != 200:
                    return False
                data = response.json()
        except Exception:
            return False

        # Version check: warn if client and server don't match
        server_version = data.get("version")
        if server_version != CLIENT_VERSION:
            _warn_version_mismatch(CLIENT_VERSION, server_version)

        # Fetch API token via SSH
        api_token = self._fetch_api_token_via_ssh(address, user)
        if not api_token:
            # Try to load from saved config as fallback
            known_pis = self.load_known_pis()
            for pi in known_pis:
                if pi.get("address") == address:
                    api_token = pi.get("api_token")
                    break

        self._state = ConnectionState(
            connected=True,
            pi_address=address,
            pi_port=port,
            pi_user=user,
            pi_hostname=data.get("hostname"),
            pi_version=data.get("version"),
            api_token=api_token,
            connected_at=datetime.utcnow().isoformat(),
            discovery_method="manual",
        )

        return True

    def connect_sync(
        self,
        address: str,
        port: int = 8421,
        user: str = "pi",
    ) -> bool:
        """Synchronous wrapper for connect."""
        import asyncio

        return asyncio.run(self.connect(address, port, user))

    def connect_ssh_only(self, address: str, user: str = "pi", port: int = 8421) -> bool:
        """Connect via SSH only, skipping the raccoon-server HTTP health check.

        Use this as a fallback when the raccoon-server is down but SSH still works
        (e.g. for raccoon update when the server is broken).

        Returns True if SSH connection succeeded.
        """
        check_paramiko_version()
        import paramiko

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=address, username=user, timeout=10)
            self._ssh_client = client
            self._state = ConnectionState(
                connected=True,
                pi_address=address,
                pi_port=port,
                pi_user=user,
            )
            return True
        except Exception:
            return False

    def disconnect(self) -> None:
        """Disconnect from the current Pi."""
        if self._ssh_client:
            self._ssh_client.close()
            self._ssh_client = None
        self._state = ConnectionState()

    def get_ssh_client(self):
        """
        Get or create an SSH client for the current connection.

        Returns:
            paramiko.SSHClient instance

        Raises:
            ParamikoVersionError: If paramiko version is too old
        """
        if not self.is_connected:
            raise RuntimeError("Not connected to a Pi")

        if self._ssh_client is None:
            # Check paramiko version before any SSH operations
            check_paramiko_version()

            import paramiko

            self._ssh_client = paramiko.SSHClient()
            self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh_client.connect(
                hostname=self._state.pi_address,
                username=self._state.pi_user,
                # Uses SSH agent or default key
            )

        return self._ssh_client

    def save_to_project(self, project_path: Path) -> None:
        """Save connection config to project's raccoon.project.yml (no sensitive data)."""
        if not (project_path / "raccoon.project.yml").exists():
            return

        from raccoon_cli.project import save_project_keys

        conn_data = ConnectionConfig(
            pi_address=self._state.pi_address,
            pi_port=self._state.pi_port,
            pi_user=self._state.pi_user,
        ).to_dict()

        save_project_keys(project_path, {"connection": conn_data})

    def load_from_project(self, project_path: Path) -> Optional[ConnectionConfig]:
        """Load connection config from project's raccoon.project.yml."""
        config_path = project_path / "raccoon.project.yml"
        if not config_path.exists():
            return None

        from raccoon_cli.yaml_utils import load_yaml

        config = load_yaml(config_path)

        connection_data = config.get("connection")
        if connection_data:
            return ConnectionConfig.from_dict(connection_data)

        return None

    def save_to_global(self) -> None:
        """Save connection to global config."""
        self.GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Load existing config
        from raccoon_cli.yaml_utils import load_yaml

        if self.GLOBAL_CONFIG_PATH.exists():
            config = load_yaml(self.GLOBAL_CONFIG_PATH)
        else:
            config = {}

        # Update known Pis
        known_pis = config.get("known_pis", [])
        existing = next(
            (p for p in known_pis if p.get("address") == self._state.pi_address), None
        )

        pi_data = {
            "address": self._state.pi_address,
            "port": self._state.pi_port,
            "hostname": self._state.pi_hostname,
            "last_seen": datetime.utcnow().isoformat(),
        }
        if self._state.api_token:
            pi_data["api_token"] = self._state.api_token

        if existing:
            existing.update(pi_data)
        else:
            known_pis.append(pi_data)

        config["known_pis"] = known_pis
        config["default_pi_user"] = self._state.pi_user

        from raccoon_cli.yaml_utils import save_yaml

        save_yaml(config, self.GLOBAL_CONFIG_PATH)

    def load_known_pis(self) -> list[dict]:
        """Load known Pis from global config."""
        if not self.GLOBAL_CONFIG_PATH.exists():
            return []

        with open(self.GLOBAL_CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}

        return config.get("known_pis", [])


# Global connection manager instance
_connection_manager: Optional[ConnectionManager] = None


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager instance."""
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
    return _connection_manager


def _warn_version_mismatch(client_version: str, server_version: str) -> None:
    """Print a version mismatch warning during connection."""
    from rich.console import Console
    console = Console(stderr=True)

    console.print()
    console.print("[yellow bold]Warning: version mismatch[/yellow bold]")
    console.print(f"  Client: [cyan]{client_version}[/cyan]  Server: [cyan]{server_version}[/cyan]")
    if client_version > server_version:
        console.print("  Run [cyan]raccoon update[/cyan] to update the Pi.")
    else:
        console.print("  Run [cyan]raccoon update[/cyan] to update your client.")
    console.print(
        "  [dim]Things may break — if they do, update both sides to the same version.[/dim]"
    )
    console.print()


def print_version_mismatch_error(error: VersionMismatchError, console=None) -> None:
    """Deprecated: version mismatch is now a warning printed during connect()."""
    pass


def print_paramiko_version_error(error: ParamikoVersionError, console=None) -> None:
    """
    Print a formatted paramiko version error to the console.

    Args:
        error: The ParamikoVersionError that was raised
        console: Optional Rich console (will create one if not provided)
    """
    if console is None:
        from rich.console import Console
        console = Console()

    console.print()
    console.print("[red bold]PARAMIKO VERSION ERROR[/red bold]")
    console.print()
    console.print(f"  Installed version: [cyan]{error.installed_version}[/cyan]")
    console.print(f"  Minimum required:  [cyan]{error.min_version}[/cyan]")
    console.print()
    console.print("[yellow]Your paramiko version is too old for SSH protocol support.[/yellow]")
    console.print("This causes 'buffer unpacking' and other SSH errors.")
    console.print()
    console.print("Update paramiko by running:")
    console.print(f"  [cyan]pip install --upgrade 'paramiko>={error.min_version}'[/cyan]")
    console.print()
    console.print("Or reinstall raccoon to get correct dependencies:")
    console.print("  [cyan]pip install --upgrade --force-reinstall raccoon[/cyan]")
