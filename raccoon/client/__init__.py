"""
Raccoon Client - Laptop-side connection and sync utilities.

This module provides:
- Connection management to Raccoon Pi servers
- SFTP-based file synchronization
- WebSocket-based output streaming
"""

from raccoon.client.connection import ConnectionManager
from raccoon.client.discovery import DiscoveredPi, check_address

__all__ = ["ConnectionManager", "DiscoveredPi", "check_address"]
