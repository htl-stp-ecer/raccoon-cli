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


@dataclass
class EncoderReading:
    """Result of reading encoder position."""

    port: int
    position: int
    success: bool
    error: Optional[str] = None


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
            response = await client.get(f"{self.base_url}/api/v1/projects/{project_id}", headers=self._auth_headers())
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
            headers=self._auth_headers(),
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
            headers=self._auth_headers(),
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
            f"{self.base_url}/api/v1/commands/{command_id}/status",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def cancel_command(self, command_id: str) -> dict:
        """Cancel a running command."""
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/commands/{command_id}/cancel",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def read_encoder(self, port: int, inverted: bool = False) -> EncoderReading:
        """
        Read the current encoder position for a motor.

        Args:
            port: Motor port number
            inverted: Whether the motor is inverted

        Returns:
            EncoderReading with the current position
        """
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/hardware/encoder/read",
            json={"port": port, "inverted": inverted},
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        data = response.json()
        return EncoderReading(
            port=data["port"],
            position=data["position"],
            success=data["success"],
            error=data.get("error"),
        )

    def get_websocket_url(self, command_id: str) -> str:
        """Get the WebSocket URL for streaming command output (includes auth token)."""
        ws_base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{ws_base}/ws/output/{command_id}"
        if self.api_token:
            url += f"?token={self.api_token}"
        return url

    # LCM Spy/Debug Methods

    async def start_lcm_spy(
        self,
        channel_patterns: Optional[list[str]] = None,
        record_to: Optional[str] = None,
    ) -> dict:
        """
        Start LCM spy session on Pi.

        Args:
            channel_patterns: List of channel patterns to filter (fnmatch style)
            record_to: Optional filename for recording

        Returns:
            Dict with status, channel_patterns, recording_file, websocket_url
        """
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/lcm/spy/start",
            json={
                "channel_patterns": channel_patterns or [],
                "record_to": record_to,
            },
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def stop_lcm_spy(self) -> dict:
        """Stop LCM spy session."""
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/lcm/spy/stop",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def get_lcm_spy_status(self) -> dict:
        """Get LCM spy session status."""
        client = self._get_client()
        response = await client.get(
            f"{self.base_url}/api/v1/lcm/spy/status",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def list_lcm_recordings(self) -> list[dict]:
        """List available LCM recordings on the Pi."""
        client = self._get_client()
        response = await client.get(
            f"{self.base_url}/api/v1/lcm/recordings",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def delete_lcm_recording(self, filename: str) -> dict:
        """Delete an LCM recording."""
        client = self._get_client()
        response = await client.delete(
            f"{self.base_url}/api/v1/lcm/recordings/{filename}",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def start_lcm_playback(
        self,
        filename: str,
        speed: float = 1.0,
        loop: bool = False,
        channel_filter: Optional[list[str]] = None,
    ) -> dict:
        """
        Start LCM playback of a recording.

        Args:
            filename: Recording filename
            speed: Playback speed multiplier (1.0 = realtime)
            loop: Whether to loop playback
            channel_filter: Optional channel patterns to filter

        Returns:
            Dict with status, filename, speed, loop
        """
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/lcm/playback/start",
            json={
                "filename": filename,
                "speed": speed,
                "loop": loop,
                "channel_filter": channel_filter or [],
            },
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def stop_lcm_playback(self) -> dict:
        """Stop LCM playback."""
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/lcm/playback/stop",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def get_lcm_playback_status(self) -> dict:
        """Get LCM playback status and progress."""
        client = self._get_client()
        response = await client.get(
            f"{self.base_url}/api/v1/lcm/playback/status",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    def get_lcm_websocket_url(self) -> str:
        """Get WebSocket URL for LCM message streaming (includes auth token)."""
        ws_base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{ws_base}/ws/lcm"
        if self.api_token:
            url += f"?token={self.api_token}"
        return url

    async def get_lcm_info(self) -> dict:
        """Get LCM spy capabilities info."""
        client = self._get_client()
        response = await client.get(
            f"{self.base_url}/api/v1/lcm/info",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def control_service(self, service_name: str, action: str) -> dict:
        """Control a systemd service on the Pi (start/stop/restart/status)."""
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/api/v1/lcm/service/control",
            json={"service_name": service_name, "action": action},
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()


def create_api_client(address: str, port: int = 8421, api_token: Optional[str] = None) -> RaccoonApiClient:
    """Create an API client for the given Pi address."""
    return RaccoonApiClient(f"http://{address}:{port}", api_token=api_token)
