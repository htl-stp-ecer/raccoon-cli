"""API route modules for the Raccoon server."""

from raccoon.server.routes.health import router as health_router
from raccoon.server.routes.commands import router as commands_router
from raccoon.server.routes.projects import router as projects_router

__all__ = ["health_router", "commands_router", "projects_router"]
