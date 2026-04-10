"""Health check and discovery endpoints."""

import socket
from datetime import datetime

from fastapi import APIRouter

from raccoon_cli.server.config import ServerConfig

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns server status, version, and basic system info.
    Used for service discovery and connection verification.
    """
    from raccoon_cli.server.app import get_config

    config = get_config()

    return {
        "status": "healthy",
        "version": config.version,
        "hostname": socket.gethostname(),
        "timestamp": datetime.utcnow().isoformat(),
        "projects_dir": str(config.projects_dir),
    }


@router.get("/")
async def root():
    """Root endpoint - redirect to health check."""
    return {
        "service": "raccoon-server",
        "message": "Raccoon Toolchain Server",
        "docs": "/docs",
        "health": "/health",
    }
