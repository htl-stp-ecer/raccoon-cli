"""
Raccoon Server - Pi-side daemon for remote toolchain execution.

This module provides:
- FastAPI-based HTTP service for command execution
- WebSocket streaming for real-time output
"""

from raccoon.server.config import ServerConfig

__all__ = ["ServerConfig"]
