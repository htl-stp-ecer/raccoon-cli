"""FastAPI application for the Raccoon server."""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from raccoon_cli.server.config import ServerConfig, load_config
from raccoon_cli.server.routes import health_router, commands_router, projects_router, hardware_router, device_router, steps_router, logs_router, version_router, calibrate_servos_router
from raccoon_cli.server.routes.lcm import router as lcm_router
from raccoon_cli.server.websocket import setup_websocket_routes
from raccoon_cli.server.websocket.lcm_stream import setup_lcm_websocket

# Global config instance
_config: Optional[ServerConfig] = None


def get_config() -> ServerConfig:
    """Get the current server configuration."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown."""
    config = get_config()

    # Ensure projects directory exists
    config.projects_dir.mkdir(parents=True, exist_ok=True)

    print(f"Raccoon Server v{config.version} starting...")
    print(f"Projects directory: {config.projects_dir}")
    print(f"Listening on http://{config.host}:{config.port}")

    yield

    print("Raccoon Server shutting down...")


def create_app(config: Optional[ServerConfig] = None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: Optional configuration override

    Returns:
        Configured FastAPI application
    """
    global _config

    if config:
        _config = config

    app = FastAPI(
        title="Raccoon Server",
        description="Remote toolchain execution service for Raccoon robotics projects",
        version=get_config().version,
        lifespan=lifespan,
    )

    # Add CORS middleware (allow all origins for local network use)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    app.include_router(health_router)
    app.include_router(projects_router)
    app.include_router(commands_router)
    app.include_router(lcm_router)
    app.include_router(hardware_router)
    app.include_router(device_router)
    app.include_router(steps_router)
    app.include_router(logs_router)
    app.include_router(version_router)
    app.include_router(calibrate_servos_router)

    # Setup WebSocket routes
    setup_websocket_routes(app)
    setup_lcm_websocket(app)

    return app


# Default app instance for uvicorn
app = create_app()
