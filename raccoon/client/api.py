"""HTTP API client for communicating with Raccoon Pi server."""

from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class ProjectInfo:
    """Information about a project on the Pi."""

    id: str
    name: str
    path: str
    has_config: bool
    last_modified: Optional[str] = None


@dataclass
class CommandResult:
    """Result of starting a command on the Pi."""

    command_id: str
    status: str
    project_id: str
    command_type: str
    started_at: str
    websocket_url: str


class RaccoonApiClient:
    """HTTP client for the Raccoon Pi server API."""

    def __init__(self, base_url: str, api_token: Optional[str] = None, timeout: float = 30.0):
        """
        Initialize the API client.

        Args:
            base_url: Base URL of the Pi server (e.g., "http://192.168.4.1:8421")
            api_token: API token for authentication (required for most endpoints)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        """Get authentication headers for API requests."""
        if self.api_token:
            return {"X-API-Token": self.api_token}
        return {}

    async def health(self) -> dict:
        """Check server health."""
        client = self._get_client()
        response = await client.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def list_projects(self) -> list[ProjectInfo]:
        """List all projects on the Pi."""
        client = self._get_client()
        response = await client.get(f"{self.base_url}/api/v1/projects", headers=self._auth_headers())
        response.raise_for_status()
        data = response.json()
        return [
            ProjectInfo(
                id=p["id"],
                name=p["name"],
                path=p["path"],
                has_config=p["has_config"],
                last_modified=p.get("last_modified"),
            )
            for p in data["projects"]
        ]

    async def get_project(self, project_id: str) -> Optional[ProjectInfo]:
        """Get details for a specific project."""
        client = self._get_client()
        try:
            response = await client.get(f"{self.base_url}/api/v1/projects/{project_id}")
            response.raise_for_status()
            p = response.json()
            return ProjectInfo(
                id=p["id"],
                name=p["name"],
                path=p["path"],
                has_config=p["has_config"],
                last_modified=p.get("last_modified"),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def run_project(
        self, project_id: str, args: list[str] = None, env: dict = None
    ) -> CommandResult:
        """
        Start running a project.

        Returns command info including WebSocket URL for output streaming.
        """
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/run/{project_id}",
            json={"args": args or [], "env": env or {}},
        )
        response.raise_for_status()
        data = response.json()
        return CommandResult(
            command_id=data["command_id"],
            status=data["status"],
            project_id=data["project_id"],
            command_type=data["command_type"],
            started_at=data["started_at"],
            websocket_url=data["websocket_url"],
        )

    async def calibrate_project(
        self, project_id: str, args: list[str] = None, env: dict = None
    ) -> CommandResult:
        """
        Start motor calibration for a project.

        Returns command info including WebSocket URL for output streaming.
        """
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/calibrate/{project_id}",
            json={"args": args or [], "env": env or {}},
        )
        response.raise_for_status()
        data = response.json()
        return CommandResult(
            command_id=data["command_id"],
            status=data["status"],
            project_id=data["project_id"],
            command_type=data["command_type"],
            started_at=data["started_at"],
            websocket_url=data["websocket_url"],
        )

    async def get_command_status(self, command_id: str) -> dict:
        """Get the status of a running command."""
        client = self._get_client()
        response = await client.get(
            f"{self.base_url}/api/v1/commands/{command_id}/status"
        )
        response.raise_for_status()
        return response.json()

    async def cancel_command(self, command_id: str) -> dict:
        """Cancel a running command."""
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/commands/{command_id}/cancel"
        )
        response.raise_for_status()
        return response.json()

    def get_websocket_url(self, command_id: str) -> str:
        """Get the WebSocket URL for streaming command output."""
        ws_base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{ws_base}/ws/output/{command_id}"


def create_api_client(address: str, port: int = 8421) -> RaccoonApiClient:
    """Create an API client for the given Pi address."""
    return RaccoonApiClient(f"http://{address}:{port}")
