from pydantic import BaseModel, Field


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class JoinGroupRequest(BaseModel):
    inviteCode: str | None = None
    groupId: int | None = Field(default=None, ge=1)


class GroupResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    role: str


class GroupMemberResponse(BaseModel):
    id: int
    email: str
    first_name: str
    last_name: str
    role: str


class GroupAvailabilityResponse(BaseModel):
    id: int
    user_id: int
    email: str
    first_name: str
    last_name: str
    day_of_week: int
    start_time: str
    end_time: str
