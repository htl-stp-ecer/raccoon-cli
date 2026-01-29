import uuid

from pydantic import BaseModel, Field


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


class Project(ProjectInDB):
    pass
