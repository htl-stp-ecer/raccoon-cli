"""FastAPI application factory for the IDE backend."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from raccoon.ide.config import Settings
from raccoon.ide.repositories.project_repository import ProjectRepository
from raccoon.ide.services.project_service import ProjectService
from raccoon.ide.services.mission_service import MissionService
from raccoon.ide.services.step_discovery_service import StepDiscoveryService
from raccoon.ide.core.project_code_gen import ProjectCodeGen

from raccoon.ide.routes import projects as projects_router
from raccoon.ide.routes import missions as missions_router
from raccoon.ide.routes import steps as steps_router
from raccoon.ide.routes import type_definitions as type_definitions_router
from raccoon.ide.routes import device as device_router
from raccoon.ide.routes import files as files_router


def create_app(project_root: Path | str = None, settings: Settings = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        project_root: Directory where projects are stored. Defaults to current working directory.
        settings: Optional Settings instance. If not provided, uses defaults.

    Returns:
        Configured FastAPI application.
    """
    if project_root is None:
        project_root = Path.cwd()
    else:
        project_root = Path(project_root)

    if settings is None:
        settings = Settings(project_root=project_root)

    app = FastAPI(
        title="Raccoon IDE",
        description="IDE backend for the Raccoon Web IDE",
        version="0.1.0",
    )

    # CORS middleware for development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Create services
    project_repository = ProjectRepository(project_root=str(project_root))
    project_codegen = ProjectCodeGen(
        project_repository=project_repository,
        template_root=settings.template_root,
    )
    project_service = ProjectService(
        project_repository=project_repository,
        project_code_gen=project_codegen,
    )
    mission_service = MissionService(
        project_repository=project_repository,
        settings=settings,
    )
    step_discovery_service = StepDiscoveryService(
        project_service=project_service,
    )

    # Store services in app state for WebSocket handlers
    app.state.project_service = project_service
    app.state.mission_service = mission_service
    app.state.step_discovery_service = step_discovery_service
    app.state.project_codegen = project_codegen

    # Override dependency injection functions
    def get_project_service() -> ProjectService:
        return project_service

    def get_mission_service() -> MissionService:
        return mission_service

    def get_step_discovery_service() -> StepDiscoveryService:
        return step_discovery_service

    def get_project_codegen() -> ProjectCodeGen:
        return project_codegen

    # Override route dependencies
    app.dependency_overrides[projects_router.get_project_service] = get_project_service
    app.dependency_overrides[missions_router.get_mission_service] = get_mission_service
    app.dependency_overrides[missions_router.get_project_codegen] = get_project_codegen
    app.dependency_overrides[steps_router.get_step_discovery_service] = get_step_discovery_service
    app.dependency_overrides[type_definitions_router.get_project_service] = get_project_service
    app.dependency_overrides[device_router.get_project_service] = get_project_service
    app.dependency_overrides[files_router.get_project_service] = get_project_service

    # Include API routes
    app.include_router(projects_router.router, prefix="/api/v1/projects", tags=["projects"])
    app.include_router(missions_router.router, prefix="/api/v1/missions", tags=["missions"])
    app.include_router(steps_router.router, prefix="/api/v1/steps", tags=["steps"])
    app.include_router(type_definitions_router.router, prefix="/api/v1/type-definitions", tags=["type-definitions"])
    app.include_router(device_router.router, prefix="/api/v1/device", tags=["device"])
    app.include_router(files_router.router, prefix="/api/v1/files", tags=["files"])

    # Health check endpoint
    @app.get("/api/v1/health")
    async def health_check():
        return {"status": "ok", "project_root": str(project_root)}

    # Mount static files for Angular frontend
    web_ide_dist = Path(__file__).parent.parent / "web-ide-dist"
    if web_ide_dist.exists():
        # Custom handler for SPA routing
        @app.get("/WebIDE/{rest_of_path:path}")
        async def serve_spa(rest_of_path: str):
            from fastapi.responses import FileResponse

            # Try to serve the exact file
            file_path = web_ide_dist / rest_of_path
            if file_path.exists() and file_path.is_file():
                return FileResponse(file_path)

            # Check if it's an asset file that should exist
            if rest_of_path and "." in rest_of_path.split("/")[-1]:
                # It's a file request, return 404 if not found
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="File not found")

            # For all other paths (SPA routes), serve index.html
            return FileResponse(web_ide_dist / "index.html")

        # Serve static assets directly
        app.mount("/WebIDE", StaticFiles(directory=web_ide_dist, html=True), name="webide")

    return app
