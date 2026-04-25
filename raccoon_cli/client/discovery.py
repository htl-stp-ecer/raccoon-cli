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
    address: str, port: int = 8421, timeout: float = 5.0,
    retries: int = 5, retry_delay: float = 0.5,
) -> Optional[DiscoveredPi]:
    """Check if a Raccoon server is running at the given address.

    Retries several times to handle ARP warm-up latency after Pi boot.
    """
    import asyncio

    for attempt in range(retries):
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
        if attempt < retries - 1:
            await asyncio.sleep(retry_delay)
    return None


def check_address_sync(
    address: str, port: int = 8421, timeout: float = 5.0
) -> Optional[DiscoveredPi]:
    """Synchronous wrapper for check_address."""
    import asyncio

    return asyncio.run(check_address(address, port, timeout))
