from pydantic import BaseModel


class MissionBase(BaseModel):
    name: str


class CreateMission(MissionBase):
    pass


class DiscoveredMission(MissionBase):
    is_setup: bool = False
    is_shutdown: bool = False
    order: int
