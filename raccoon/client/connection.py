"""Connection state management for Raccoon client."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


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
    """Connection configuration stored in project or global config."""

    pi_address: Optional[str] = None
    pi_port: int = 8421
    pi_user: str = "pi"
    remote_path: Optional[str] = None
    auto_connect: bool = True
    api_token: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for YAML serialization."""
        d = {
            "pi_address": self.pi_address,
            "pi_port": self.pi_port,
            "pi_user": self.pi_user,
            "remote_path": self.remote_path,
            "auto_connect": self.auto_connect,
        }
        if self.api_token:
            d["api_token"] = self.api_token
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionConfig":
        """Create from dictionary."""
        return cls(
            pi_address=data.get("pi_address"),
            pi_port=data.get("pi_port", 8421),
            pi_user=data.get("pi_user", "pi"),
            remote_path=data.get("remote_path"),
            auto_connect=data.get("auto_connect", True),
            api_token=data.get("api_token"),
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
        """
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
        """
        import httpx

        # Verify connection to server (health endpoint is public)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"http://{address}:{port}/health")
                if response.status_code != 200:
                    return False
                data = response.json()
        except Exception:
            return False

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
        """
        if not self.is_connected:
            raise RuntimeError("Not connected to a Pi")

        if self._ssh_client is None:
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
        """Save connection config to project's raccoon.project.yml."""
        config_path = project_path / "raccoon.project.yml"
        if not config_path.exists():
            return

        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        config["connection"] = ConnectionConfig(
            pi_address=self._state.pi_address,
            pi_port=self._state.pi_port,
            pi_user=self._state.pi_user,
            api_token=self._state.api_token,
        ).to_dict()

        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    def load_from_project(self, project_path: Path) -> Optional[ConnectionConfig]:
        """Load connection config from project's raccoon.project.yml."""
        config_path = project_path / "raccoon.project.yml"
        if not config_path.exists():
            return None

        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        connection_data = config.get("connection")
        if connection_data:
            return ConnectionConfig.from_dict(connection_data)

        return None

    def save_to_global(self) -> None:
        """Save connection to global config."""
        self.GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Load existing config
        if self.GLOBAL_CONFIG_PATH.exists():
            with open(self.GLOBAL_CONFIG_PATH) as f:
                config = yaml.safe_load(f) or {}
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

        with open(self.GLOBAL_CONFIG_PATH, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)

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
