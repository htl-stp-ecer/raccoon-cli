"""
Raccoon Server - Pi-side daemon for remote toolchain execution.

This module provides:
- FastAPI-based HTTP service for command execution
- WebSocket streaming for real-time output
- Project management
- Hardware access APIs
"""

# Configuration
from raccoon.server.config import ServerConfig

# Services
from raccoon.server.services import CommandExecutor, ProjectManager

# Routes
from raccoon.server.routes import (
    health_router,
    commands_router,
    projects_router,
    hardware_router,
)

# WebSocket
from raccoon.server.websocket import setup_websocket_routes

__all__ = [
    # Configuration
    "ServerConfig",
    # Services
    "CommandExecutor",
    "ProjectManager",
    # Routes
    "health_router",
    "commands_router",
    "projects_router",
    "hardware_router",
    # WebSocket
    "setup_websocket_routes",
]
