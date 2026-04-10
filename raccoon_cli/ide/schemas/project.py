"""Pydantic models for IDE project resources and connection metadata."""

import uuid

from pydantic import BaseModel, Field


class ProjectConnection(BaseModel):
    """Saved connection details for a Raspberry Pi target."""

    pi_address: str | None = None
    pi_port: int | None = None
    pi_user: str | None = None
    remote_path: str | None = None
    auto_connect: bool | None = None


class ProjectBase(BaseModel):
    """Shared fields for project create/update payloads."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_\-\s]+$"
    )


class ProjectCreate(ProjectBase):
    """Payload for creating or renaming a project record."""

    pass


class ProjectInDB(ProjectBase):
    """Persisted project record stored by the IDE backend."""

    uuid: uuid.UUID
    connection: ProjectConnection | None = None


class Project(ProjectInDB):
    """API response model for project resources."""

    pass
