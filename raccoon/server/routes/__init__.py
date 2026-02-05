"""API route modules for the Raccoon server."""

from raccoon.server.routes.health import router as health_router
from raccoon.server.routes.commands import router as commands_router
from raccoon.server.routes.projects import router as projects_router
from raccoon.server.routes.hardware import router as hardware_router
from raccoon.server.routes.device import router as device_router

__all__ = ["health_router", "commands_router", "projects_router", "hardware_router", "device_router"]
