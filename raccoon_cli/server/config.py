"""Server configuration management."""

import os
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from raccoon_cli import __version__


# Token file location (readable via SSH by authorized users)
TOKEN_FILE_PATH = Path.home() / ".raccoon" / "api_token"


@dataclass
class ServerConfig:
    """Configuration for the Raccoon server daemon."""

    # Network settings
    host: str = "0.0.0.0"
    port: int = 8421

    # Directory where projects are stored on the Pi
    projects_dir: Path = field(default_factory=lambda: Path.home() / "programs")

    # API token for authentication
    api_token: Optional[str] = None

    # Server version (from package)
    version: str = field(default_factory=lambda: __version__)

    @classmethod
    def from_file(cls, path: Path) -> "ServerConfig":
        """Load configuration from a YAML file."""
        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(
            host=data.get("host", "0.0.0.0"),
            port=data.get("port", 8421),
            projects_dir=Path(data.get("projects_dir", Path.home() / "programs")),
        )

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Load configuration from environment variables."""
        return cls(
            host=os.environ.get("RACCOON_HOST", "0.0.0.0"),
            port=int(os.environ.get("RACCOON_PORT", "8421")),
            projects_dir=Path(
                os.environ.get("RACCOON_PROJECTS_DIR", Path.home() / "programs")
            ),
        )

    def to_dict(self) -> dict:
        """Convert configuration to dictionary."""
        return {
            "host": self.host,
            "port": self.port,
            "projects_dir": str(self.projects_dir),
            "version": self.version,
        }

    def save(self, path: Path) -> None:
        """Save configuration to a YAML file."""
        from raccoon_cli.yaml_utils import save_yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        save_yaml(self.to_dict(), path)


# Default config file locations
SYSTEM_CONFIG_PATH = Path("/etc/raccoon/server.yml")
USER_CONFIG_PATH = Path.home() / ".raccoon" / "server.yml"


def get_or_create_api_token() -> str:
    """
    Get the API token, creating one if it doesn't exist.

    The token is stored in ~/.raccoon/api_token and can be read
    by clients who have SSH access to the Pi.
    """
    TOKEN_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if TOKEN_FILE_PATH.exists():
        token = TOKEN_FILE_PATH.read_text().strip()
        if token:
            return token

    # Generate new token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE_PATH.write_text(token)
    # Restrict permissions to owner only
    TOKEN_FILE_PATH.chmod(0o600)

    return token


def load_config() -> ServerConfig:
    """Load configuration from files and environment, with priority."""
    # Start with defaults
    config = ServerConfig()

    # Load from system config if exists
    if SYSTEM_CONFIG_PATH.exists():
        config = ServerConfig.from_file(SYSTEM_CONFIG_PATH)

    # Override with user config if exists
    if USER_CONFIG_PATH.exists():
        user_config = ServerConfig.from_file(USER_CONFIG_PATH)
        # Merge user config over system config
        config = user_config

    # Override with environment variables
    env_config = ServerConfig.from_env()
    if os.environ.get("RACCOON_PORT"):
        config.port = env_config.port
    if os.environ.get("RACCOON_HOST"):
        config.host = env_config.host
    if os.environ.get("RACCOON_PROJECTS_DIR"):
        config.projects_dir = env_config.projects_dir

    # Load or create API token
    config.api_token = get_or_create_api_token()

    return config
