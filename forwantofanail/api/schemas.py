from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LoginRequest(BaseModel):
    commander_name: str


class ActionCreateRequest(BaseModel):
    kind: Literal["move", "forage", "attack", "besiege"]
    destination_h3: str | None = None
    target_h3: str | None = None
    target_army_id: str | None = None
    target_stronghold_id: str | None = None


class ActionPlanRequest(BaseModel):
    kind: Literal["march", "forage"]
    path: list[str] = []


class MessageCreateRequest(BaseModel):
    recipient_id: str
    content: str
    priority: str = "normal"


class TimeAdvanceRequest(BaseModel):
    steps: int = 1
    execute_actions: bool = True


class TimePayload(BaseModel):
    day: int
    watch: int
    watch_label: str


class StandingFollowRoadUpdateRequest(BaseModel):
    enabled: bool


class ArmyManagementCommanderCreateRequest(BaseModel):
    name: str
    title: str


class ArmyManagementArmySideRequest(BaseModel):
    army_id: str | None = None
    name: str
    commander_id: str | None = None
    supply_current: int | None = None
    detachment_ids: list[str] = []
    new_commander: ArmyManagementCommanderCreateRequest | None = None


class ArmyManagementRightTargetRequest(BaseModel):
    mode: Literal["existing", "new"]
    army_id: str | None = None


class ArmyManagementApplyRequest(BaseModel):
    baseline_hash: str
    left_army: ArmyManagementArmySideRequest
    right_target: ArmyManagementRightTargetRequest
    right_army: ArmyManagementArmySideRequest
