"""
Raccoon Client - Laptop-side connection and sync utilities.

This module provides:
- Connection management to Raccoon Pi servers
- Rsync-based file synchronization
- WebSocket-based output streaming
- SSH key management
- API client for remote operations
"""

# Connection management
from raccoon.client.connection import (
    ConnectionManager,
    ConnectionState,
    ConnectionConfig,
    get_connection_manager,
    VersionMismatchError,
    print_version_mismatch_error,
    ParamikoVersionError,
    print_paramiko_version_error,
    check_paramiko_version,
)

# Discovery
from raccoon.client.discovery import (
    DiscoveredPi,
    check_address,
    check_address_sync,
)

# API client
from raccoon.client.api import (
    RaccoonApiClient,
    create_api_client,
    ProjectInfo,
    CommandResult,
    EncoderReading,
)

# Rsync sync
from raccoon.client.sftp_sync import (
    RsyncSync,
    SyncDirection,
    SyncResult,
    SyncOptions,
    load_raccoonignore,
)

# Output handling
from raccoon.client.output_handler import (
    OutputHandler,
    stream_command_output,
)

# SSH key management
from raccoon.client.ssh_keys import (
    SSHKeyManager,
    setup_ssh_key_interactive,
)

__all__ = [
    # Connection management
    "ConnectionManager",
    "ConnectionState",
    "ConnectionConfig",
    "get_connection_manager",
    "VersionMismatchError",
    "print_version_mismatch_error",
    "ParamikoVersionError",
    "print_paramiko_version_error",
    "check_paramiko_version",
    # Discovery
    "DiscoveredPi",
    "check_address",
    "check_address_sync",
    # API client
    "RaccoonApiClient",
    "create_api_client",
    "ProjectInfo",
    "CommandResult",
    "EncoderReading",
    # Rsync sync
    "RsyncSync",
    "SyncDirection",
    "SyncResult",
    "SyncOptions",
    "load_raccoonignore",
    # Output handling
    "OutputHandler",
    "stream_command_output",
    # SSH key management
    "SSHKeyManager",
    "setup_ssh_key_interactive",
]
