"""Transport contract status of this Pi: one endpoint for CLI, Web-IDE and UI."""

from fastapi import APIRouter

from raccoon_cli import contract_status

router = APIRouter(prefix="/api/v1", tags=["contract"])


@router.get("/contract")
async def get_contract() -> dict:
    """Expected hash and transport version from the bundle file, dev mode, and every
    component's startup-check result with its activity (read from the status files)."""
    return contract_status.local_summary()
