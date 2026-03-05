"""Compact mission list models returned by the IDE mission endpoints."""

from pydantic import BaseModel


class MissionBase(BaseModel):
    """Shared fields for mission payloads identified by name."""

    name: str


class CreateMission(MissionBase):
    """Payload model for creating a new mission."""

    pass


class DiscoveredMission(MissionBase):
    """Mission metadata extracted from project configuration."""

    is_setup: bool = False
    is_shutdown: bool = False
    order: int
