"""Authentication utilities for the Raccoon server."""

from fastapi import Header, HTTPException, status

from raccoon_cli.server.config import get_or_create_api_token


async def require_auth(x_api_token: str = Header(alias="X-API-Token")) -> str:
    """
    Dependency that requires a valid API token.

    The token must be provided in the X-API-Token header.
    Clients obtain this token by reading ~/.raccoon/api_token via SSH.
    """
    expected_token = get_or_create_api_token()

    if not x_api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API token. Use 'raccoon connect' to authenticate.",
        )

    if x_api_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token. Use 'raccoon connect' to re-authenticate.",
        )

    return x_api_token
