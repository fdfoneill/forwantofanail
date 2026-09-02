from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Callable

import h3
from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from forwantofanail.api import routes
from forwantofanail.api.schemas import (
    ActionCreateRequest,
    ActionPlanRequest,
    ArmyManagementApplyRequest,
    ArmyManagementArmySideRequest,
    ArmyManagementCommanderCreateRequest,
    ArmyManagementRightTargetRequest,
    MessageCreateRequest,
)
from forwantofanail.core.models import (
    Action,
    Alert,
    AlertRecipient,
    Army,
    Commander,
    Location,
    Message,
    StandingOrder,
    Stronghold,
    TerrainType,
)
from forwantofanail.core.scenario_catalog import load_historical_stronghold_catalog
from forwantofanail.mechanics.location_description import (
    _bearing_word,
    describe_army_location,
    describe_march_stage,
    morale_condition_word,
)
from forwantofanail.mechanics.movement import (
    calculate_move_watches_from_origin,
    list_valid_destinations_from_origin,
)
from forwantofanail.mechanics.navigation import RouteNotFoundError, build_route_summary, find_route_path
from forwantofanail.mechanics.supply import supply_stats

from .schemas import (
    CancelOrderInput,
    EmptyInput,
    GetOrderOptionsInput,
    ListActivityInput,
    ReadActivityInput,
    ReorganizeArmiesInput,
    SearchStrongholdsInput,
    SendLetterInput,
    SetStandingOrdersInput,
    SubmitOrderInput,
    SummarizeRouteInput,
    SurveyMapInput,
)
from .security import find_matching_handle, matches, opaque_handle


TOOLSET_VERSION = "1.1.0"
_H3_VALUE = re.compile(r"(?i)\b[0-9a-f]{15}\b")


@dataclass(frozen=True)
class ToolContext:
    session: Session
    commander_id: int
    session_binding: str
    idempotency_key: str | None = None
    request_identity: str | None = None


class ToolInvocationError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        refresh_tool: str | None = None,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        message = _H3_VALUE.sub("[map reference omitted]", message)
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.refresh_tool = refresh_tool
        self.details = None if not details else [
            {
                key: _H3_VALUE.sub("[map reference omitted]", str(item))
                for key, item in detail.items()
            }
            for detail in details
        ]

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.refresh_tool:
            value["refresh_with"] = self.refresh_tool
        if self.details:
            value["details"] = self.details
        return value


def _as_of(clock: Any) -> str:
    payload = routes._clock_payload(clock)
    return f"{payload['calendar_date']}, {payload['watch_label']} watch"


def _state_fingerprint(ctx: ToolContext) -> str:
    session = ctx.session
    clock = routes._get_or_create_clock(session)
    army = routes._find_commander_army(session, ctx.commander_id)
    actions = (
        session.query(Action)
        .filter(Action.commander_id == ctx.commander_id, Action.state.in_(routes.ACTIVE_ACTION_STATES))
        .order_by(Action.action_id.asc())
        .all()
    )
    standing = session.get(StandingOrder, ctx.commander_id)
    siege = routes._find_active_siege_for_commander(session, ctx.commander_id)
    mobility = [
        (int(det.detachment_id), int(det.warrior_count or 0), int(det.wagon_count or 0), bool(det.is_cavalry))
        for det in sorted(army.detachments, key=lambda row: row.detachment_id)
    ]
    action_state = [
        (int(action.action_id), action.kind, action.state, action.parameters_json)
        for action in actions
    ]
    return opaque_handle(
        "state",
        ctx.session_binding,
        ctx.commander_id,
        int(clock.world_tick),
        army.location_id,
        bool(army.is_embarked),
        float(army.noncombattant_percent or 0.0),
        mobility,
        action_state,
        int(siege.siege_id) if siege is not None else None,
        bool(standing.follow_road_enabled) if standing else False,
        bool(standing.forced_march_enabled) if standing else False,
    )


def _require_state(ctx: ToolContext, supplied: str) -> str:
    current = _state_fingerprint(ctx)
    if not matches(supplied, current):
        raise ToolInvocationError(
            "stale_state",
            "The tactical situation has changed; obtain fresh order options before acting.",
            status_code=409,
            refresh_tool="fwoan_get_order_options",
        )
    return current


def _result(ctx: ToolContext, tool: str, data: dict[str, Any], *, dynamic: bool = True) -> dict[str, Any]:
    clock = routes._get_or_create_clock(ctx.session) if dynamic else None
    return {
        "tool": tool,
        "toolset_version": TOOLSET_VERSION,
        "as_of": _as_of(clock) if clock is not None else None,
        "state_token": _state_fingerprint(ctx) if dynamic else None,
        "data": _redact_map_references(data),
    }


def _redact_map_references(value: Any) -> Any:
    """Keep implementation coordinates out of every server-generated result.

    Player-authored letter bodies are explicitly marked untrusted and remain
    byte-for-byte game correspondence; hosts must delimit them from instructions.
    """
    if isinstance(value, dict):
        is_letter = value.get("source") == "player_letter" and value.get("untrusted_content") is True
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if is_letter and key == "content":
                cleaned[key] = item
            else:
                cleaned[key] = _redact_map_references(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_map_references(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_map_references(item) for item in value]
    if isinstance(value, str):
        return _H3_VALUE.sub("[map reference omitted]", value)
    return value


def _safe_action(session: Session, action: Action | None, army: Army) -> dict[str, Any] | None:
    if action is None:
        return None
    target = routes._brief_action_target(session, action, army)
    eta = routes._brief_action_eta(action)
    return {
        "order_ref": routes._action_ref(action.action_id),
        "kind": action.kind,
        "state": action.state,
        "target": target or None,
        "expected_completion": eta or None,
    }


def get_situation(ctx: ToolContext, _payload: EmptyInput) -> dict[str, Any]:
    session = ctx.session
    clock = routes._get_or_create_clock(session)
    army = routes._find_commander_army(session, ctx.commander_id)
    current = routes._get_current_action_row(session, ctx.commander_id)
    active_orders = (
        session.query(Action)
        .filter(Action.commander_id == ctx.commander_id, Action.state.in_(routes.ACTIVE_ACTION_STATES))
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )
    standing = routes._serialize_standing_orders(session.get(StandingOrder, ctx.commander_id))
    radius = routes._environs_radius_for_army(army)
    environs = routes._serialize_environs(
        session,
        army.location_id,
        radius,
        exclude_army_id=army.army_id,
        viewer_commander_id=ctx.commander_id,
        viewer_army=army,
    )
    unread_alerts, high_alerts = routes._brief_unread_alert_counts(
        session, commander_id=ctx.commander_id, world_tick=int(clock.world_tick)
    )
    infantry = sum(int(det.warrior_count or 0) for det in army.detachments if not det.is_cavalry)
    cavalry = sum(int(det.warrior_count or 0) for det in army.detachments if det.is_cavalry)
    stats = supply_stats(army)
    local_armies: list[dict[str, Any]] = []
    local_strongholds: list[dict[str, Any]] = []
    for cell in environs["cells"]:
        for other in cell.get("other_armies", []):
            item = {key: value for key, value in other.items() if key != "army_id"}
            item["army_ref"] = other.get("army_id")
            item["bearing"] = _bearing_word(army.location_id, cell["h3"])
            local_armies.append(item)
        stronghold = cell.get("stronghold")
        if stronghold:
            local_strongholds.append(
                {
                    "stronghold_ref": stronghold["id"],
                    "name": stronghold["name"],
                    "type": stronghold["type"],
                    "controller": stronghold["faction"],
                    "distance_leagues": routes._grid_distance(army.location_id, cell["h3"]),
                    "bearing": _bearing_word(army.location_id, cell["h3"]),
                    "garrison_strength": stronghold["defender_strength"],
                    "under_siege": stronghold["under_siege"],
                }
            )
    time_payload = routes._clock_payload(clock)
    return _result(
        ctx,
        "fwoan_get_situation",
        {
            "brief": routes._commander_brief_text(session, ctx.commander_id),
            "time": {
                "date": time_payload["calendar_date"],
                "day": time_payload["day"],
                "watch": time_payload["watch_label"],
            },
            "army": {
                "army_ref": routes._army_ref(army.army_id),
                "name": army.army_name,
                "strength": infantry + cavalry,
                "infantry": infantry,
                "cavalry": cavalry,
                "supply": int(army.army_supply or 0),
                "supply_capacity": int(stats.capacity or 0),
                "days_of_supply": stats.days_estimate,
                "morale": morale_condition_word(army.army_morale),
                "location": describe_army_location(session, army.location_id),
                "scouting_radius_leagues": radius,
            },
            "orders": {
                "current": _safe_action(session, current, army),
                "active_and_queued": [_safe_action(session, action, army) for action in active_orders],
                "standing": {
                    "follow_road": bool(standing["follow_road"]["enabled"]),
                    "forced_march": bool(standing["forced_march"]["enabled"]),
                },
            },
            "attention": {
                "unread_letters": routes._brief_unread_letter_count(
                    session, commander_id=ctx.commander_id, clock=clock
                ),
                "unread_alerts": unread_alerts,
                "high_importance_alerts": high_alerts,
            },
            "local_situation": {
                "strongholds": sorted(local_strongholds, key=lambda item: (item["distance_leagues"], item["name"])),
                "other_armies": local_armies,
            },
        },
    )


def _activity_rows(ctx: ToolContext, payload: ListActivityInput) -> list[dict[str, Any]]:
    session = ctx.session
    clock = routes._get_or_create_clock(session)
    items: list[dict[str, Any]] = []
    if payload.activity_type in {"all", "letters"}:
        incoming = and_(
            Message.recipient_id == ctx.commander_id,
            Message.status == "received",
            Message.delivery_tick <= clock.world_tick,
        )
        outgoing = Message.sender_commander_id == ctx.commander_id
        query = session.query(Message).options(joinedload(Message.recipient)).filter(or_(incoming, outgoing))
        for message in query.all():
            direction = "sent" if message.sender_commander_id == ctx.commander_id else "received"
            if payload.letter_direction != "all" and payload.letter_direction != direction:
                continue
            if payload.unread_only and (direction == "sent" or message.is_read):
                continue
            available = int(message.sent_tick if direction == "sent" else message.delivery_tick)
            item = {
                "activity_ref": routes._message_ref(message.message_id),
                "activity_type": "letter",
                "direction": direction,
                "counterparty": (
                    routes._message_recipient_display_name(message)
                    if direction == "sent"
                    else routes._message_sender_display_name(message)
                ),
                "priority": message.priority,
                "sent": routes._to_watch_stamp(message.sent_day, message.sent_watch),
                "unread": direction == "received" and not bool(message.is_read),
                "source": "player_letter",
                "untrusted_content": True,
                "_sort": (available, 0, int(message.message_id)),
            }
            items.append(item)
    if payload.activity_type in {"all", "alerts"} and payload.letter_direction == "all":
        rows = (
            session.query(Alert, AlertRecipient)
            .join(AlertRecipient, AlertRecipient.alert_id == Alert.alert_id)
            .filter(
                AlertRecipient.commander_id == ctx.commander_id,
                AlertRecipient.available_tick <= clock.world_tick,
                Alert.signal_kind == "event",
            )
            .all()
        )
        now = datetime.now(timezone.utc)
        for alert, receipt in rows:
            if payload.unread_only and receipt.read_at is not None:
                continue
            if receipt.delivered_at is None:
                receipt.delivered_at = now
            items.append(
                {
                    "activity_ref": routes._alert_ref(alert.alert_id),
                    "activity_type": "alert",
                    "category": alert.category,
                    "importance": alert.importance,
                    "summary": alert.message,
                    "available": routes._to_watch_stamp(alert.delivered_day, alert.delivered_watch),
                    "unread": receipt.read_at is None,
                    "source": "game_event",
                    "untrusted_content": False,
                    "_sort": (int(receipt.available_tick), 1, int(alert.alert_id)),
                }
            )
    return sorted(items, key=lambda item: item["_sort"], reverse=True)


def list_activity(ctx: ToolContext, payload: ListActivityInput) -> dict[str, Any]:
    items = _activity_rows(ctx, payload)
    start = 0
    if payload.cursor:
        candidates = [
            (opaque_handle("activity", ctx.session_binding, ctx.commander_id, item["_sort"]), index)
            for index, item in enumerate(items)
        ]
        index = find_matching_handle(payload.cursor, candidates)
        if index is None:
            raise ToolInvocationError("option_expired", "That activity cursor is no longer valid.", status_code=409)
        if payload.direction == "older":
            start = int(index) + 1
        else:
            items = list(reversed(items[: int(index)]))
            start = 0
    page = items[start : start + payload.limit]
    output = []
    for item in page:
        clean = {key: value for key, value in item.items() if key != "_sort"}
        clean["cursor"] = opaque_handle("activity", ctx.session_binding, ctx.commander_id, item["_sort"])
        output.append(clean)
    ctx.session.commit()  # delivery acknowledgement only; listing never marks read
    return _result(
        ctx,
        "fwoan_list_activity",
        {
            "items": output,
            "has_more": start + len(page) < len(items),
            "next_cursor": output[-1]["cursor"] if output else None,
        },
    )


def read_activity(ctx: ToolContext, payload: ReadActivityInput) -> dict[str, Any]:
    ref = payload.activity_ref
    try:
        if ref.startswith("msg_"):
            value = routes.get_message(ref, commander_id=ctx.commander_id, session=ctx.session)
            value.update({"source": "player_letter", "untrusted_content": True})
        elif ref.startswith("alt_"):
            alert_id = routes._parse_alert_ref(ref)
            row = (
                ctx.session.query(Alert, AlertRecipient)
                .join(AlertRecipient, AlertRecipient.alert_id == Alert.alert_id)
                .filter(Alert.alert_id == alert_id, AlertRecipient.commander_id == ctx.commander_id)
                .one_or_none()
            )
            clock = routes._get_or_create_clock(ctx.session, for_update=True)
            if row is None or int(row[1].available_tick) > int(clock.world_tick):
                raise ToolInvocationError("not_found", "Activity not found.", status_code=404)
            alert, receipt = row
            now = datetime.now(timezone.utc)
            receipt.delivered_at = receipt.delivered_at or now
            receipt.read_at = receipt.read_at or now
            ctx.session.commit()
            value = routes._serialize_alert(alert, receipt)
            value.update({"source": "game_event", "untrusted_content": False})
        else:
            raise ToolInvocationError("not_found", "Activity not found.", status_code=404)
    except HTTPException as exc:
        raise _from_http(exc, default_code="not_found") from exc
    return _result(ctx, "fwoan_read_activity", {"activity": value})


def list_correspondents(ctx: ToolContext, _payload: EmptyInput) -> dict[str, Any]:
    values = routes.list_correspondents(commander_id=ctx.commander_id, session=ctx.session)
    return _result(ctx, "fwoan_list_correspondents", {"correspondents": values})


def send_letter(ctx: ToolContext, payload: SendLetterInput) -> dict[str, Any]:
    try:
        value = routes.send_message(
            MessageCreateRequest(
                recipient_id=payload.recipient_ref,
                content=payload.content,
                priority=payload.priority,
            ),
            commander_id=ctx.commander_id,
            session=ctx.session,
            idempotency_key=_mutation_key(ctx),
        )
    except HTTPException as exc:
        raise _from_http(exc) from exc
    return _result(ctx, "fwoan_send_letter", {"receipt": value})


def _catalog_rows() -> list[dict[str, Any]]:
    catalog = load_historical_stronghold_catalog()
    return sorted(catalog["strongholds"], key=lambda item: (str(item["name"]).casefold(), item["id"]))


def search_strongholds(ctx: ToolContext, payload: SearchStrongholdsInput) -> dict[str, Any]:
    rows = _catalog_rows()
    query = str(payload.query or "").strip().casefold()
    faction = str(payload.historical_faction or "").strip().casefold()
    kind = str(payload.stronghold_type or "").strip().casefold()
    rows = [
        row for row in rows
        if (not query or query in str(row["name"]).casefold() or query in str(row.get("historical_gloss") or "").casefold())
        and (not faction or faction == str(row["historical_faction"]).casefold())
        and (not kind or kind == str(row["stronghold_type"]).casefold())
    ]
    start = 0
    if payload.cursor:
        index = find_matching_handle(
            payload.cursor,
            [(opaque_handle("stronghold_cursor", ctx.session_binding, row["id"]), i) for i, row in enumerate(rows)],
        )
        if index is None:
            raise ToolInvocationError("option_expired", "That search cursor is no longer valid.", status_code=409)
        start = int(index) + 1
    page = rows[start : start + payload.limit]
    clean = [
        {
            "stronghold_ref": row["id"],
            "name": row["name"],
            "historical_faction": row["historical_faction"],
            "type": row["stronghold_type"],
            "historical_gloss": row.get("historical_gloss"),
        }
        for row in page
    ]
    next_cursor = opaque_handle("stronghold_cursor", ctx.session_binding, page[-1]["id"]) if page else None
    return _result(
        ctx,
        "fwoan_search_strongholds",
        {"items": clean, "has_more": start + len(page) < len(rows), "next_cursor": next_cursor},
        dynamic=False,
    )


def survey_map(ctx: ToolContext, payload: SurveyMapInput) -> dict[str, Any]:
    session = ctx.session
    army = routes._find_commander_army(session, ctx.commander_id)
    if payload.center == "current":
        center_h3 = army.location_id
        center_name = describe_army_location(session, center_h3)
    else:
        try:
            center_stronghold = session.get(Stronghold, routes._parse_stronghold_ref(payload.center))
        except HTTPException as exc:
            raise _from_http(exc, default_code="not_found") from exc
        if center_stronghold is None:
            raise ToolInvocationError("not_found", "Stronghold not found.", status_code=404)
        center_h3 = center_stronghold.location_id
        center_name = center_stronghold.stronghold_name
    try:
        disk = set(h3.grid_disk(center_h3, payload.radius))
    except Exception as exc:
        raise ToolInvocationError("invalid_arguments", "The requested static survey cannot be constructed.") from exc
    locations = (
        session.query(Location)
        .options(joinedload(Location.terrain_type))
        .filter(Location.location_id.in_(disk))
        .order_by(Location.location_id.asc())
        .all()
    )
    terrain_counts: dict[str, int] = {}
    road_bearings: set[str] = set()
    river_bearings: set[str] = set()
    for location in locations:
        terrain = str(location.terrain_type.terrain_name if location.terrain_type else "unknown").lower()
        terrain_counts[terrain] = terrain_counts.get(terrain, 0) + 1
        bearing = _bearing_word(center_h3, location.location_id)
        if location.is_road and bearing:
            road_bearings.add(bearing)
        if terrain == "river" and bearing:
            river_bearings.add(bearing)
    catalog = {row["id"]: row for row in _catalog_rows()}
    static_strongholds = []
    for stronghold in session.query(Stronghold).filter(Stronghold.location_id.in_(disk)).order_by(Stronghold.stronghold_id).all():
        row = catalog.get(routes._stronghold_ref(stronghold.stronghold_id))
        if row is None:
            continue
        static_strongholds.append(
            {
                "stronghold_ref": row["id"],
                "name": row["name"],
                "historical_faction": row["historical_faction"],
                "type": row["stronghold_type"],
                "historical_gloss": row.get("historical_gloss"),
                "distance_leagues": routes._grid_distance(center_h3, stronghold.location_id),
                "bearing": _bearing_word(center_h3, stronghold.location_id),
            }
        )
    terrain_phrase = ", ".join(f"{count} {name}" for name, count in sorted(terrain_counts.items()))
    prose = f"Around {center_name}, the known map contains {terrain_phrase}."
    if road_bearings:
        prose += f" Roads extend toward {', '.join(sorted(road_bearings))}."
    if static_strongholds:
        prose += " Known strongholds include " + ", ".join(
            f"{item['name']} ({item['distance_leagues']} leagues {item['bearing'] or 'away'})"
            for item in static_strongholds
        ) + "."
    return _result(
        ctx,
        "fwoan_survey_map",
        {
            "center": center_name,
            "radius_leagues": payload.radius,
            "prose": prose,
            "terrain": [{"type": name, "cells": count} for name, count in sorted(terrain_counts.items())],
            "road_directions": sorted(road_bearings),
            "river_directions": sorted(river_bearings),
            "strongholds": static_strongholds,
            "information_scope": "scenario_static",
        },
    )


def summarize_route(ctx: ToolContext, payload: SummarizeRouteInput) -> dict[str, Any]:
    session = ctx.session
    army = routes._find_commander_army(session, ctx.commander_id)
    try:
        destination = session.get(Stronghold, routes._parse_stronghold_ref(payload.destination_ref))
        origin = None if payload.origin_ref == "current" else session.get(
            Stronghold, routes._parse_stronghold_ref(payload.origin_ref)
        )
        if destination is None or (payload.origin_ref != "current" and origin is None):
            raise ToolInvocationError("not_found", "Stronghold not found.", status_code=404)
        value = build_route_summary(
            session, army=army, origin=origin, destination=destination, allow_off_road=payload.allow_off_road
        )
    except HTTPException as exc:
        raise _from_http(exc, default_code="not_found") from exc
    except RouteNotFoundError as exc:
        raise ToolInvocationError("not_found", str(exc), status_code=404) from exc
    return _result(ctx, "fwoan_summarize_route", {"route": value})


def _move_handle(ctx: ToolContext, state: str, prefix: list[str], destination: str) -> str:
    return opaque_handle("move_option", ctx.session_binding, ctx.commander_id, state, prefix, destination)


def _legal_next_cells(ctx: ToolContext, army: Army, prefix: list[str]) -> list[str]:
    origin = prefix[-1] if prefix else army.location_id
    try:
        valid = list_valid_destinations_from_origin(ctx.session, army.army_id, origin)
    except ValueError:
        return []
    clock = routes._get_or_create_clock(ctx.session)
    budget = routes._remaining_march_watch_budget_for_watch(
        int(clock.watch), army, routes._forced_march_enabled_for_army(ctx.session, army)
    )
    return sorted(
        cell for cell in valid
        if not routes._is_enemy_occupied(ctx.session, destination_h3=cell, moving_army=army)
        and routes._path_watches_for_army(ctx.session, army, army.location_id, prefix + [cell]) <= budget
    )


def _resolve_steps(ctx: ToolContext, army: Army, state: str, handles: list[str]) -> list[str]:
    prefix: list[str] = []
    for supplied in handles:
        candidates = [
            (_move_handle(ctx, state, prefix, cell), cell)
            for cell in _legal_next_cells(ctx, army, prefix)
        ]
        cell = find_matching_handle(supplied, candidates)
        if cell is None:
            raise ToolInvocationError(
                "option_expired",
                "A selected march step is no longer legal; obtain fresh order options.",
                status_code=409,
                refresh_tool="fwoan_get_order_options",
            )
        prefix.append(str(cell))
    return prefix


def _target_options(ctx: ToolContext, state: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attacks_raw = routes.get_valid_attack_targets(
        staged_path=None, commander_id=ctx.commander_id, session=ctx.session
    ).get("targets", [])
    besieges_raw = routes.get_valid_besiege_targets(
        staged_path=None, commander_id=ctx.commander_id, session=ctx.session
    ).get("targets", [])
    attacks = []
    for target in attacks_raw:
        hidden = (target.get("target_h3"), target.get("target_army_id"))
        attacks.append(
            {
                "option": opaque_handle("attack_option", ctx.session_binding, ctx.commander_id, state, hidden),
                "label": target.get("label"),
                "faction": target.get("faction"),
                "mode": "assault" if routes._army_is_in_stronghold(ctx.session, routes._find_commander_army(ctx.session, ctx.commander_id)) else "attack",
                "_hidden": hidden,
            }
        )
    besieges = []
    for target in besieges_raw:
        hidden = (target.get("target_h3"), target.get("stronghold_id"))
        besieges.append(
            {
                "option": opaque_handle("siege_option", ctx.session_binding, ctx.commander_id, state, hidden),
                "stronghold_ref": target.get("stronghold_id"),
                "name": target.get("stronghold_name"),
                "faction": target.get("faction"),
                "_hidden": hidden,
            }
        )
    return attacks, besieges


def get_order_options(ctx: ToolContext, payload: GetOrderOptionsInput) -> dict[str, Any]:
    state = _state_fingerprint(ctx)
    if payload.state_token and not matches(payload.state_token, state):
        raise ToolInvocationError(
            "stale_state", "The supplied observation is stale.", status_code=409, refresh_tool="fwoan_get_order_options"
        )
    army = routes._find_commander_army(ctx.session, ctx.commander_id)
    prefix = _resolve_steps(ctx, army, state, payload.staged_steps)
    origin = prefix[-1] if prefix else army.location_id
    next_moves = []
    for cell in _legal_next_cells(ctx, army, prefix):
        location = ctx.session.get(Location, cell)
        next_moves.append(
            {
                "option": _move_handle(ctx, state, prefix, cell),
                "direction": _bearing_word(origin, cell),
                "description": describe_march_stage(ctx.session, origin, cell),
                "terrain": str(location.terrain_type.terrain_name if location and location.terrain_type else "unknown").lower(),
                "on_road": bool(location.is_road) if location else False,
                "watch_cost": calculate_move_watches_from_origin(ctx.session, army.army_id, origin, cell),
            }
        )
    attacks, besieges = _target_options(ctx, state)
    clock = routes._get_or_create_clock(ctx.session)
    daily_budget = routes._remaining_march_watch_budget_for_watch(
        int(clock.watch), army, routes._forced_march_enabled_for_army(ctx.session, army)
    )
    staged_cost = routes._path_watches_for_army(ctx.session, army, army.location_id, prefix)
    current = routes._get_current_action_row(ctx.session, ctx.commander_id)
    active_orders = (
        ctx.session.query(Action)
        .filter(Action.commander_id == ctx.commander_id, Action.state.in_(routes.ACTIVE_ACTION_STATES))
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )
    routing = current is not None and current.kind == "rout" and current.state == "in_progress"
    forage_allowed = not routes._army_is_under_siege(ctx.session, army) and int(clock.watch) in {
        int(routes.Watch.NIGHT), int(routes.Watch.MATIN)
    }
    legal_kinds: list[str] = []
    if not routing:
        legal_kinds.append("hold")
        if next_moves or prefix:
            legal_kinds.append("march")
        if forage_allowed:
            legal_kinds.append("forage")
        if attacks:
            legal_kinds.append("attack")
        if besieges:
            legal_kinds.append("besiege")
    recommendation: dict[str, Any] | None = None
    if payload.route_goal_ref:
        try:
            goal = ctx.session.get(Stronghold, routes._parse_stronghold_ref(payload.route_goal_ref))
        except HTTPException as exc:
            raise _from_http(exc, default_code="not_found") from exc
        if goal is None:
            raise ToolInvocationError("not_found", "Route goal not found.", status_code=404)
        try:
            path, _costs = find_route_path(
                ctx.session, army=army, origin=None, destination=goal, allow_off_road=payload.allow_off_road
            )
            visible = set(h3.grid_disk(army.location_id, routes._environs_radius_for_army(army)))
            recommended_cells: list[str] = []
            recommended_handles: list[str] = []
            for cell in path[1:]:
                if cell not in visible or cell not in _legal_next_cells(ctx, army, recommended_cells):
                    break
                recommended_handles.append(_move_handle(ctx, state, recommended_cells, cell))
                recommended_cells.append(cell)
            total_cost = routes._path_watches_for_army(ctx.session, army, army.location_id, recommended_cells)
            recommendation = {
                "goal": goal.stronghold_name,
                "steps": recommended_handles,
                "stage_count": len(recommended_handles),
                "endpoint": (
                    describe_army_location(ctx.session, recommended_cells[-1])
                    if recommended_cells else describe_army_location(ctx.session, army.location_id)
                ),
                "watch_cost": total_cost,
                "remaining_watch_budget": max(0, daily_budget - total_cost),
                "note": "Further orders require fresh scouting." if len(recommended_cells) < max(0, len(path) - 1) else None,
            }
        except RouteNotFoundError as exc:
            raise ToolInvocationError("not_found", str(exc), status_code=404) from exc
    return _result(
        ctx,
        "fwoan_get_order_options",
        {
            "current_order": _safe_action(ctx.session, current, army),
            "active_and_queued_orders": [_safe_action(ctx.session, action, army) for action in active_orders],
            "legal_order_kinds": legal_kinds,
            "forage": {"eligible": forage_allowed},
            "staged": {
                "step_count": len(prefix),
                "endpoint": describe_army_location(ctx.session, origin),
                "watch_cost": staged_cost,
                "remaining_watch_budget": max(0, daily_budget - staged_cost),
                "next_moves": next_moves,
            },
            "attack_targets": [{key: value for key, value in item.items() if key != "_hidden"} for item in attacks],
            "siege_targets": [{key: value for key, value in item.items() if key != "_hidden"} for item in besieges],
            "recommended_march": recommendation,
            "handle_warning": "Opaque references are for tool calls only and must never be quoted in letters.",
        },
    )


def submit_order(ctx: ToolContext, payload: SubmitOrderInput) -> dict[str, Any]:
    state = _require_state(ctx, payload.state_token)
    order = payload.order
    army = routes._find_commander_army(ctx.session, ctx.commander_id)
    key = _mutation_key(ctx)
    try:
        if order.kind == "march":
            cells = _resolve_steps(ctx, army, state, order.steps)
            receipt = routes.plan_actions(
                ActionPlanRequest(kind="march", path=cells),
                commander_id=ctx.commander_id,
                session=ctx.session,
                idempotency_key=key,
            )
        elif order.kind == "hold":
            receipt = routes.plan_actions(
                ActionPlanRequest(kind="march", path=[]),
                commander_id=ctx.commander_id,
                session=ctx.session,
                idempotency_key=key,
            )
        elif order.kind == "forage":
            receipt = routes.plan_actions(
                ActionPlanRequest(kind="forage", path=[]),
                commander_id=ctx.commander_id,
                session=ctx.session,
                idempotency_key=key,
            )
        else:
            attacks, besieges = _target_options(ctx, state)
            pool = besieges if order.kind == "besiege" else attacks
            selected = find_matching_handle(
                str(order.target_option), [(item["option"], item["_hidden"]) for item in pool]
            )
            if selected is None:
                raise ToolInvocationError(
                    "option_expired", "That target is no longer legal.", status_code=409, refresh_tool="fwoan_get_order_options"
                )
            target_h3, entity_ref = selected
            request = (
                ActionCreateRequest(kind="besiege", target_h3=target_h3, target_stronghold_id=entity_ref)
                if order.kind == "besiege"
                else ActionCreateRequest(kind="attack", target_h3=target_h3, target_army_id=entity_ref)
            )
            receipt = routes.create_action(
                request,
                commander_id=ctx.commander_id,
                session=ctx.session,
                idempotency_key=key,
            )
    except HTTPException as exc:
        raise _from_http(exc) from exc
    return _result(ctx, "fwoan_submit_order", {"receipt": receipt})


def cancel_order(ctx: ToolContext, payload: CancelOrderInput) -> dict[str, Any]:
    _require_state(ctx, payload.state_token)
    try:
        receipt = routes.cancel_action(
            payload.order_ref,
            commander_id=ctx.commander_id,
            session=ctx.session,
            idempotency_key=_mutation_key(ctx),
        )
    except HTTPException as exc:
        raise _from_http(exc) from exc
    return _result(ctx, "fwoan_cancel_order", {"receipt": receipt})


def set_standing_orders(ctx: ToolContext, payload: SetStandingOrdersInput) -> dict[str, Any]:
    _require_state(ctx, payload.state_token)

    def operation():
        routes._lock_commander_scope(ctx.session, ctx.commander_id)
        clock = routes._get_or_create_clock(ctx.session, for_update=True)
        army = routes._find_commander_army(ctx.session, ctx.commander_id)
        current = routes._get_current_action_row(ctx.session, ctx.commander_id)
        if current is not None and current.kind == "rout" and current.state == "in_progress":
            raise HTTPException(status_code=409, detail="Army is routing; standing orders cannot be changed.")
        standing = routes._get_or_create_standing_order(ctx.session, ctx.commander_id)
        if payload.follow_road is not None:
            requested_follow = bool(payload.follow_road)
            if bool(standing.follow_road_enabled) != requested_follow:
                standing.follow_road_enabled = requested_follow
                standing.last_report = (
                    "Standing order issued: follow road."
                    if requested_follow
                    else "Standing order rescinded: follow road."
                )
                standing.last_report_day = None if requested_follow else clock.day
                standing.last_report_watch = None if requested_follow else clock.watch
                routes._create_alert(
                    ctx.session,
                    recipient_commander_id=ctx.commander_id,
                    alert_type="action",
                    signal_kind="event",
                    category="standing-order",
                    importance="normal",
                    message=standing.last_report,
                    created_day=clock.day,
                    created_watch=clock.watch,
                )
        if payload.forced_march is not None:
            if (
                bool(standing.forced_march_enabled)
                and not payload.forced_march
                and routes._forced_march_is_locked_for_watch(int(clock.watch))
            ):
                raise HTTPException(status_code=400, detail="Forced march cannot be disabled in this watch.")
            requested_forced = bool(payload.forced_march)
            if bool(standing.forced_march_enabled) != requested_forced:
                standing.forced_march_enabled = requested_forced
                if requested_forced:
                    routes._create_alert(
                        ctx.session,
                        recipient_commander_id=ctx.commander_id,
                        alert_type="action",
                        signal_kind="event",
                        category="standing-order",
                        importance="normal",
                        message="Standing order issued: forced march.",
                        created_day=clock.day,
                        created_watch=clock.watch,
                    )
        standing.updated_at = datetime.now(timezone.utc)
        _ = army
        return routes._serialize_standing_orders(standing)

    try:
        receipt = routes._run_idempotent_mutation(
            ctx.session,
            actor_scope=f"commander:{ctx.commander_id}",
            route="agent-standing-orders",
            idempotency_key=_mutation_key(ctx),
            payload=payload,
            operation=operation,
        )
    except HTTPException as exc:
        raise _from_http(exc) from exc
    return _result(ctx, "fwoan_set_standing_orders", {"standing_orders": receipt})


def _organization_state(ctx: ToolContext) -> tuple[Army, list[Army], str]:
    army = (
        ctx.session.query(Army)
        .options(joinedload(Army.commander), joinedload(Army.detachments))
        .filter(Army.commander_id == ctx.commander_id)
        .first()
    )
    if army is None:
        raise ToolInvocationError("not_found", "No commanded army was found.", status_code=404)
    eligible = routes._eligible_management_armies(ctx.session, army)
    baseline = routes._army_management_snapshot_hash(army, eligible)
    token = opaque_handle("organization", ctx.session_binding, ctx.commander_id, baseline)
    return army, eligible, token


def _clean_management_army(session: Session, army: Army) -> dict[str, Any]:
    value = routes._serialize_management_army(army)
    value.pop("location_h3", None)
    value["location"] = describe_army_location(session, army.location_id)
    value["morale"] = morale_condition_word(army.army_morale)
    return value


def inspect_organization(ctx: ToolContext, _payload: EmptyInput) -> dict[str, Any]:
    army, eligible, token = _organization_state(ctx)
    return _result(
        ctx,
        "fwoan_inspect_organization",
        {
            "organization_token": token,
            "primary": _clean_management_army(ctx.session, army),
            "eligible_colocated_armies": [_clean_management_army(ctx.session, item) for item in eligible],
            "new_army_template": routes._army_management_new_army_template(ctx.session, army.army_faction),
            "constraints": [
                "Every listed detachment must appear exactly once in the desired final arrangement.",
                "Garrison supply cannot be transferred; field-army supply may be lost when capacity falls.",
                "All participating armies must remain colocated and of the same faction.",
            ],
        },
    )


def reorganize_armies(ctx: ToolContext, payload: ReorganizeArmiesInput) -> dict[str, Any]:
    army, eligible, current_token = _organization_state(ctx)
    if not matches(payload.organization_token, current_token):
        raise ToolInvocationError(
            "stale_state",
            "The organization has changed; inspect it again before reorganizing.",
            status_code=409,
            refresh_tool="fwoan_inspect_organization",
        )
    baseline = routes._army_management_snapshot_hash(army, eligible)

    def side(value: Any) -> ArmyManagementArmySideRequest:
        new_commander = None
        if value.new_commander_name or value.new_commander_title:
            if not value.new_commander_name or not value.new_commander_title:
                raise ToolInvocationError("invalid_arguments", "A new commander requires both a name and title.")
            new_commander = ArmyManagementCommanderCreateRequest(
                name=value.new_commander_name, title=value.new_commander_title
            )
        return ArmyManagementArmySideRequest(
            army_id=value.army_ref,
            name=value.name,
            commander_id=value.commander_ref,
            supply_current=value.supply,
            detachment_ids=value.detachment_refs,
            new_commander=new_commander,
        )

    left = side(payload.primary)
    right = side(payload.secondary) if payload.secondary is not None else None
    original_supply = int(army.army_supply or 0)
    if left.supply_current is not None and int(left.supply_current) < original_supply and not payload.accept_supply_loss:
        raise ToolInvocationError(
            "not_allowed",
            "This arrangement reduces the primary field army's supply. Repeat with accept_supply_loss=true to confirm.",
        )
    request = ArmyManagementApplyRequest(
        baseline_hash=baseline,
        left_army=left,
        right_target=ArmyManagementRightTargetRequest(
            mode=payload.secondary_mode,
            army_id=payload.secondary.army_ref if payload.secondary is not None else None,
        ),
        right_army=right,
    )
    try:
        receipt = routes.apply_army_management(
            request,
            commander_id=ctx.commander_id,
            session=ctx.session,
            idempotency_key=_mutation_key(ctx),
        )
    except HTTPException as exc:
        raise _from_http(exc) from exc
    return _result(ctx, "fwoan_reorganize_armies", {"receipt": receipt})


def _mutation_key(ctx: ToolContext) -> str:
    value = ctx.idempotency_key or ctx.request_identity
    if not value:
        raise ToolInvocationError("invalid_arguments", "This mutation requires an idempotency identity.")
    return str(value)


def _from_http(exc: HTTPException, *, default_code: str = "not_allowed") -> ToolInvocationError:
    status = int(exc.status_code)
    if status == 404:
        code = "not_found"
    elif status == 409:
        code = "conflict"
    elif status == 503:
        code = "retryable"
    elif status in {400, 422}:
        code = default_code
    else:
        code = default_code
    detail = exc.detail
    message = detail if isinstance(detail, str) else "The request could not be completed under the current rules."
    # Internal transport errors may contain implementation coordinates. Never
    # return those details through the model-facing facade.
    if "h3" in message.casefold() or "cell" in message.casefold():
        message = "The requested option is not legal in the current situation."
    return ToolInvocationError(code, message, status_code=status)


HANDLERS: dict[str, Callable[[ToolContext, Any], dict[str, Any]]] = {
    "fwoan_get_situation": get_situation,
    "fwoan_list_activity": list_activity,
    "fwoan_read_activity": read_activity,
    "fwoan_list_correspondents": list_correspondents,
    "fwoan_send_letter": send_letter,
    "fwoan_search_strongholds": search_strongholds,
    "fwoan_survey_map": survey_map,
    "fwoan_summarize_route": summarize_route,
    "fwoan_get_order_options": get_order_options,
    "fwoan_submit_order": submit_order,
    "fwoan_cancel_order": cancel_order,
    "fwoan_set_standing_orders": set_standing_orders,
    "fwoan_inspect_organization": inspect_organization,
    "fwoan_reorganize_armies": reorganize_armies,
}
