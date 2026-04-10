"""Simple health check for Raccoon Pi servers."""

from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class DiscoveredPi:
    """Represents a discovered Raccoon Pi server."""

    address: str
    port: int
    hostname: str
    version: Optional[str] = None


async def check_address(
    address: str, port: int = 8421, timeout: float = 5.0
) -> Optional[DiscoveredPi]:
    """
    Check if a Raccoon server is running at the given address.

    Args:
        address: IP address or hostname
        port: Server port (default 8421)
        timeout: Connection timeout in seconds

    Returns:
        DiscoveredPi if server is found, None otherwise
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"http://{address}:{port}/health")
            if response.status_code == 200:
                data = response.json()
                return DiscoveredPi(
                    address=address,
                    port=port,
                    hostname=data.get("hostname", "unknown"),
                    version=data.get("version"),
                )
    except Exception:
        pass
    return None


def check_address_sync(
    address: str, port: int = 8421, timeout: float = 5.0
) -> Optional[DiscoveredPi]:
    """Synchronous wrapper for check_address."""
    import asyncio

    return asyncio.run(check_address(address, port, timeout))
