from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Type

from pydantic import BaseModel, ValidationError

from .schemas import (
    ActivityListResult,
    ActivityReadResult,
    CancelOrderInput,
    CorrespondentsResult,
    EmptyInput,
    GetOrderOptionsInput,
    ListActivityInput,
    MapSurveyResult,
    OrderOptionsResult,
    OrganizationResult,
    ReadActivityInput,
    ReceiptResult,
    ReorganizeArmiesInput,
    RouteSummaryResult,
    StrategicOverviewInput,
    StrategicOverviewResult,
    SearchStrongholdsInput,
    SendLetterInput,
    SetStandingOrdersInput,
    SituationResult,
    StandingOrdersResult,
    StrongholdSearchResult,
    SubmitOrderInput,
    SummarizeRouteInput,
    SurveyMapInput,
)
from .services import HANDLERS, TOOLSET_VERSION, ToolContext, ToolInvocationError, _from_http


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    classification: Literal["read", "mutation"]

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "classification": self.classification,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


_DEFINITIONS = (
    ToolDefinition("fwoan_get_situation", "Observe the commander's current diegetic situation, condition, orders, local intelligence, and unread counts.", EmptyInput, SituationResult, "read"),
    ToolDefinition("fwoan_list_activity", "Page letters and event alerts available to this commander without marking them read.", ListActivityInput, ActivityListResult, "read"),
    ToolDefinition("fwoan_read_activity", "Open one letter or alert. Incoming letters and alerts become read; outgoing letters never reveal delivery information.", ReadActivityInput, ActivityReadResult, "mutation"),
    ToolDefinition("fwoan_list_correspondents", "List commanders to whom this commander may legally send letters.", EmptyInput, CorrespondentsResult, "read"),
    ToolDefinition("fwoan_send_letter", "Send an in-game letter of 1 to 4,000 characters to a legal correspondent.", SendLetterInput, ReceiptResult, "mutation"),
    ToolDefinition("fwoan_search_strongholds", "Search scenario-known strongholds and their historical, non-live descriptions.", SearchStrongholdsInput, StrongholdSearchResult, "read"),
    ToolDefinition("fwoan_survey_map", "Survey static terrain, rivers, roads, and historical strongholds around the current army or a known stronghold.", SurveyMapInput, MapSurveyResult, "read"),
    ToolDefinition("fwoan_summarize_route", "Summarize a static, diegetic route to a stronghold without revealing map coordinates.", SummarizeRouteInput, RouteSummaryResult, "read"),
    ToolDefinition("fwoan_get_strategic_overview", "Consult the scenario-static strategic atlas: historical faction regions, major cities and reviewed choke points, corridors, frontiers, and map edges. Use this before choosing a strategic destination.", StrategicOverviewInput, StrategicOverviewResult, "read"),
    ToolDefinition("fwoan_get_order_options", "Orient tactically: list legal orders and opaque next-step or target options, optionally drafting a march toward a route goal.", GetOrderOptionsInput, OrderOptionsResult, "read"),
    ToolDefinition(
        "fwoan_submit_order",
        "Submit exactly one typed order using the state token and opaque options from a fresh order-options result. "
        "Put the typed variant under order: march uses steps; attack, assault, sortie, and besiege use target_option; "
        "hold and forage use neither. Call this only after reviewing the order-options result, and treat it as "
        "successful only when the result says ok=true.",
        SubmitOrderInput,
        ReceiptResult,
        "mutation",
    ),
    ToolDefinition("fwoan_cancel_order", "Cancel one active or queued order after current-state revalidation.", CancelOrderInput, ReceiptResult, "mutation"),
    ToolDefinition("fwoan_set_standing_orders", "Atomically update follow-road and forced-march standing orders.", SetStandingOrdersInput, StandingOrdersResult, "mutation"),
    ToolDefinition("fwoan_inspect_organization", "Inspect exact detachments, supplies, commanders, colocated friendly armies, and organization constraints.", EmptyInput, OrganizationResult, "read"),
    ToolDefinition("fwoan_reorganize_armies", "Apply a desired final two-army or army-and-garrison organization using a fresh organization token.", ReorganizeArmiesInput, ReceiptResult, "mutation"),
)

_BY_NAME = {definition.name: definition for definition in _DEFINITIONS}


def list_tools() -> tuple[ToolDefinition, ...]:
    return _DEFINITIONS


def get_tool(name: str) -> ToolDefinition | None:
    return _BY_NAME.get(name)


def catalog() -> dict[str, Any]:
    return {
        "toolset": "for-want-of-a-nail-commander-tools",
        "version": TOOLSET_VERSION,
        "tools": [definition.catalog_entry() for definition in _DEFINITIONS],
    }


def invoke(name: str, raw_arguments: Any, ctx: ToolContext) -> dict[str, Any]:
    definition = get_tool(name)
    if definition is None:
        raise ToolInvocationError("not_found", "Unknown commander tool.", status_code=404)
    try:
        arguments = definition.input_model.model_validate(raw_arguments if raw_arguments is not None else {})
    except ValidationError as exc:
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())) or "arguments",
                "message": str(error.get("msg", "Invalid value.")),
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        raise ToolInvocationError(
            "invalid_arguments",
            "Arguments do not match the tool schema. Correct the listed fields and retry only if the intended action has not succeeded.",
            status_code=422,
            details=details,
        ) from exc
    def run_handler() -> dict[str, Any]:
        return HANDLERS[name](ctx, arguments)

    if definition.classification == "mutation":
        # This facade-level record deliberately surrounds state-token checks. A
        # retry after a successful response was lost therefore recovers the
        # original result even though the successful mutation changed state.
        from forwantofanail.api import routes

        identity = ctx.idempotency_key or ctx.request_identity
        if not identity:
            raise ToolInvocationError(
                "invalid_arguments", "This mutation requires an idempotency identity.", status_code=400
            )
        try:
            value = routes._run_idempotent_mutation(
                ctx.session,
                actor_scope=f"commander:{ctx.commander_id}",
                route=f"agent-tool:{name}",
                idempotency_key=identity,
                payload=arguments,
                operation=run_handler,
            )
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                raise _from_http(exc, default_code="conflict") from exc
            raise
    else:
        value = run_handler()
    try:
        return definition.output_model.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:  # pragma: no cover - programming error guardrail
        raise RuntimeError(f"Tool {name} returned an invalid result") from exc
