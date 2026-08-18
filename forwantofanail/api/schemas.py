from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    commander_name: str = Field(min_length=1, max_length=100)


class ClaimRequest(BaseModel):
    commander_id: str
    game_password: str = Field(min_length=1, max_length=512)
    client_kind: Literal["browser", "api"] = "browser"


class ActionCreateRequest(BaseModel):
    kind: Literal["move", "forage", "attack", "besiege"]
    destination_h3: str | None = None
    target_h3: str | None = None
    target_army_id: str | None = None
    target_stronghold_id: str | None = None


class ActionPlanRequest(BaseModel):
    kind: Literal["march", "forage"]
    path: list[str] = Field(default_factory=list, max_length=25)


class MessageCreateRequest(BaseModel):
    recipient_id: str
    content: str = Field(min_length=1, max_length=4000)
    priority: Literal["low", "normal", "high"] = "normal"

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class TimeAdvanceRequest(BaseModel):
    steps: int = Field(default=1, ge=1, le=25)
    execute_actions: bool = True


class TimePayload(BaseModel):
    day: int
    watch: int
    watch_label: str


class StandingFollowRoadUpdateRequest(BaseModel):
    enabled: bool


class ArmyManagementCommanderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=100)


class ArmyManagementArmySideRequest(BaseModel):
    army_id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    commander_id: str | None = None
    supply_current: int | None = None
    detachment_ids: list[str] = Field(default_factory=list)
    new_commander: ArmyManagementCommanderCreateRequest | None = None


class ArmyManagementRightTargetRequest(BaseModel):
    mode: Literal["existing", "new", "none"]
    army_id: str | None = None


class ArmyManagementApplyRequest(BaseModel):
    baseline_hash: str
    left_army: ArmyManagementArmySideRequest
    right_target: ArmyManagementRightTargetRequest
    right_army: ArmyManagementArmySideRequest | None = None


class AlertIdsRequest(BaseModel):
    alert_ids: list[str] = Field(min_length=1, max_length=200)
