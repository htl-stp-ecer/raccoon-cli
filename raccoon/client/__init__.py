"""
Raccoon Client - Laptop-side connection and sync utilities.

This module provides:
- Connection management to Raccoon Pi servers
- SFTP-based file synchronization
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

# SFTP sync
from raccoon.client.sftp_sync import (
    SftpSync,
    SyncDirection,
    SyncResult,
    SyncOptions,
    HashCache,
    RemoteManifest,
    load_raccoonignore,
    REMOTE_MANIFEST_FILENAME,
    LOCAL_CACHE_DIR,
    LOCAL_CACHE_FILENAME,
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
    # SFTP sync
    "SftpSync",
    "SyncDirection",
    "SyncResult",
    "SyncOptions",
    "HashCache",
    "RemoteManifest",
    "load_raccoonignore",
    "REMOTE_MANIFEST_FILENAME",
    "LOCAL_CACHE_DIR",
    "LOCAL_CACHE_FILENAME",
    # Output handling
    "OutputHandler",
    "stream_command_output",
    # SSH key management
    "SSHKeyManager",
    "setup_ssh_key_interactive",
]
