import uuid

from pydantic import BaseModel, Field


class ProjectConnection(BaseModel):
    pi_address: str | None = None
    pi_port: int | None = None
    pi_user: str | None = None
    remote_path: str | None = None
    auto_connect: bool | None = None


class ProjectBase(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_\-\s]+$"
    )


class ProjectCreate(ProjectBase):
    pass


class ProjectInDB(ProjectBase):
    uuid: uuid.UUID
    connection: ProjectConnection | None = None


class Project(ProjectInDB):
    pass
