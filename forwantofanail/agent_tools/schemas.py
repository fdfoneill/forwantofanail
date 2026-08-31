from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyInput(StrictModel):
    pass


class ToolResult(StrictModel):
    tool: str
    toolset_version: str
    as_of: str | None = None
    state_token: str | None = None
    data: dict[str, Any]


class SituationData(StrictModel):
    brief: str
    time: dict[str, Any]
    army: dict[str, Any]
    orders: dict[str, Any]
    attention: dict[str, Any]
    local_situation: dict[str, Any]


class ActivityListData(StrictModel):
    items: list[dict[str, Any]]
    has_more: bool
    next_cursor: str | None


class ActivityReadData(StrictModel):
    activity: dict[str, Any]


class CorrespondentsData(StrictModel):
    correspondents: list[dict[str, Any]]


class ReceiptData(StrictModel):
    receipt: dict[str, Any]


class StrongholdSearchData(StrictModel):
    items: list[dict[str, Any]]
    has_more: bool
    next_cursor: str | None


class MapSurveyData(StrictModel):
    center: str
    radius_leagues: int
    prose: str
    terrain: list[dict[str, Any]]
    road_directions: list[str]
    river_directions: list[str]
    strongholds: list[dict[str, Any]]
    information_scope: Literal["scenario_static"]


class RouteSummaryData(StrictModel):
    route: dict[str, Any]


class OrderOptionsData(StrictModel):
    current_order: dict[str, Any] | None
    active_and_queued_orders: list[dict[str, Any] | None]
    legal_order_kinds: list[str]
    forage: dict[str, Any]
    staged: dict[str, Any]
    attack_targets: list[dict[str, Any]]
    siege_targets: list[dict[str, Any]]
    recommended_march: dict[str, Any] | None
    handle_warning: str


class StandingOrdersData(StrictModel):
    standing_orders: dict[str, Any]


class OrganizationData(StrictModel):
    organization_token: str
    primary: dict[str, Any]
    eligible_colocated_armies: list[dict[str, Any]]
    new_army_template: dict[str, Any]
    constraints: list[str]


class SituationResult(ToolResult):
    data: SituationData


class ActivityListResult(ToolResult):
    data: ActivityListData


class ActivityReadResult(ToolResult):
    data: ActivityReadData


class CorrespondentsResult(ToolResult):
    data: CorrespondentsData


class ReceiptResult(ToolResult):
    data: ReceiptData


class StrongholdSearchResult(ToolResult):
    data: StrongholdSearchData


class MapSurveyResult(ToolResult):
    data: MapSurveyData


class RouteSummaryResult(ToolResult):
    data: RouteSummaryData


class OrderOptionsResult(ToolResult):
    data: OrderOptionsData


class StandingOrdersResult(ToolResult):
    data: StandingOrdersData


class OrganizationResult(ToolResult):
    data: OrganizationData


class ListActivityInput(StrictModel):
    cursor: str | None = Field(default=None, max_length=160)
    direction: Literal["older", "newer"] = "older"
    limit: int = Field(default=20, ge=1, le=100)
    activity_type: Literal["all", "letters", "alerts"] = "all"
    letter_direction: Literal["all", "received", "sent"] = "all"
    unread_only: bool = False


class ReadActivityInput(StrictModel):
    activity_ref: str = Field(min_length=1, max_length=64)


class SendLetterInput(StrictModel):
    recipient_ref: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4000)
    priority: Literal["low", "normal", "high"] = "normal"


class SearchStrongholdsInput(StrictModel):
    query: str | None = Field(default=None, max_length=100)
    historical_faction: str | None = Field(default=None, max_length=100)
    stronghold_type: str | None = Field(default=None, max_length=100)
    cursor: str | None = Field(default=None, max_length=160)
    limit: int = Field(default=25, ge=1, le=100)


class SurveyMapInput(StrictModel):
    center: Literal["current"] | str = "current"
    radius: int = Field(default=6, ge=1, le=20)


class SummarizeRouteInput(StrictModel):
    destination_ref: str = Field(min_length=1, max_length=64)
    origin_ref: str = Field(default="current", min_length=1, max_length=64)
    allow_off_road: bool = False


class GetOrderOptionsInput(StrictModel):
    state_token: str | None = Field(default=None, max_length=160)
    staged_steps: list[str] = Field(default_factory=list, max_length=25)
    route_goal_ref: str | None = Field(default=None, max_length=64)
    allow_off_road: bool = False


class SubmitOrderInput(StrictModel):
    state_token: str = Field(min_length=1, max_length=160)
    kind: Literal["march", "hold", "forage", "attack", "assault", "sortie", "besiege"]
    steps: list[str] = Field(default_factory=list, max_length=25)
    target_option: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "march" and not self.steps:
            raise ValueError("march requires at least one opaque step option")
        if self.kind != "march" and self.steps:
            raise ValueError("steps are accepted only for march orders")
        needs_target = self.kind in {"attack", "assault", "sortie", "besiege"}
        if needs_target != bool(self.target_option):
            raise ValueError("target_option is required exactly for attack, assault, sortie, and besiege")
        return self


class CancelOrderInput(StrictModel):
    state_token: str = Field(min_length=1, max_length=160)
    order_ref: str = Field(min_length=1, max_length=64)


class SetStandingOrdersInput(StrictModel):
    state_token: str = Field(min_length=1, max_length=160)
    follow_road: bool | None = None
    forced_march: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.follow_road is None and self.forced_march is None:
            raise ValueError("at least one standing-order setting is required")
        return self


class OrganizationSideInput(StrictModel):
    army_ref: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    commander_ref: str | None = Field(default=None, max_length=64)
    supply: int | None = Field(default=None, ge=0)
    detachment_refs: list[str] = Field(default_factory=list)
    new_commander_name: str | None = Field(default=None, max_length=100)
    new_commander_title: str | None = Field(default=None, max_length=100)


class ReorganizeArmiesInput(StrictModel):
    organization_token: str = Field(min_length=1, max_length=160)
    primary: OrganizationSideInput
    secondary_mode: Literal["existing", "new", "none"]
    secondary: OrganizationSideInput | None = None
    accept_supply_loss: bool = False

    @model_validator(mode="after")
    def validate_secondary(self):
        if self.secondary_mode == "none" and self.secondary is not None:
            raise ValueError("secondary must be omitted when secondary_mode is none")
        if self.secondary_mode != "none" and self.secondary is None:
            raise ValueError("secondary is required for an existing or new secondary army")
        return self
