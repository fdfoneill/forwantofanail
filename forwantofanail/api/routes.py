from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import os
import json
import random
import secrets
from typing import Any

import h3
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from forwantofanail.api.schemas import (
    ActionCreateRequest,
    LoginRequest,
    MessageCreateRequest,
    TimeAdvanceRequest,
)
from forwantofanail.core.database import create_session
from forwantofanail.core.models import (
    Action,
    Army,
    AuthToken,
    Commander,
    GameClock,
    Location,
    Message,
    Movement,
    Stronghold,
    TerrainType,
)
from forwantofanail.mechanics.movement import calculate_move_watches, list_valid_destinations
from forwantofanail.mechanics.supply import consume_supply_for_all_armies, supply_stats
from forwantofanail.mechanics.time import Watch

router = APIRouter(prefix="/v1")

WATCH_LABELS = {
    Watch.NIGHT: "night",
    Watch.MATIN: "matin",
    Watch.PRIME: "prime",
    Watch.NOON: "midday",
    Watch.VESPER: "vesper",
}
ACTIVE_ACTION_STATES = {"queued", "in_progress"}
SCENARIO_EPOCH = date(2000, 1, 1)
MESSAGE_LOSS_PROBABILITY = 0.0


def _commander_ref(commander_id: int) -> str:
    return f"cmd_{commander_id}"


def _army_ref(army_id: int) -> str:
    return f"army_{army_id}"


def _detachment_ref(detachment_id: int) -> str:
    return f"det_{detachment_id}"


def _stronghold_ref(stronghold_id: int) -> str:
    return f"sh_{stronghold_id}"


def _action_ref(action_id: int) -> str:
    return f"act_{action_id}"


def _message_ref(message_id: int) -> str:
    return f"msg_{message_id}"


def _parse_action_ref(value: str) -> int:
    if value.startswith("act_"):
        value = value[4:]
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="action_id must be an integer or act_<id>") from exc


def _parse_message_ref(value: str) -> int:
    if value.startswith("msg_"):
        value = value[4:]
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="message_id must be an integer or msg_<id>") from exc


def _parse_commander_ref(value: str) -> int:
    if value.startswith("cmd_"):
        value = value[4:]
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="recipient_id must be an integer or cmd_<id>") from exc


def _commander_display_name(commander: Commander) -> str:
    title = (commander.commander_title or "").strip()
    name = (commander.commander_name or "").strip()
    return f"{title} {name}".strip() if title else name


def _message_sender_display_name(message: Message) -> str:
    if message.sender_commander is not None:
        return _commander_display_name(message.sender_commander)
    return message.sender_name


def _get_session():
    session = create_session()
    try:
        yield session
    finally:
        session.close()


def _get_or_create_clock(session: Session) -> GameClock:
    clock = session.get(GameClock, 1)
    if clock is None:
        clock = GameClock(singleton_id=1, day=1, watch=int(Watch.MATIN))
        session.add(clock)
        session.commit()
        session.refresh(clock)
    return clock


def _clock_payload(clock: GameClock) -> dict[str, int | str]:
    watch_enum = Watch(int(clock.watch))
    return {
        "day": clock.day,
        "watch": int(clock.watch),
        "watch_label": WATCH_LABELS[watch_enum],
    }


def _to_watch_stamp(day: int, watch: int) -> dict[str, int]:
    return {"day": day, "watch": watch}


def _is_delivered_filter(day: int, watch: int):
    return or_(
        Message.delivery_day < day,
        and_(Message.delivery_day == day, Message.delivery_watch <= watch),
    )


def _grid_distance(origin_h3: str, destination_h3: str) -> int:
    try:
        if hasattr(h3, "grid_distance"):
            return int(h3.grid_distance(origin_h3, destination_h3))
        if hasattr(h3, "h3_distance"):
            return int(h3.h3_distance(origin_h3, destination_h3))
    except Exception:
        return 0
    return 0


def _message_travel_watches(origin_h3: str, destination_h3: str) -> int:
    distance = max(0, _grid_distance(origin_h3, destination_h3))
    return max(1, int(math.ceil(distance / 4.0)))


def _commander_location_h3(session: Session, commander_id: int) -> str | None:
    army = session.query(Army).filter(Army.commander_id == commander_id).first()
    if army is None:
        return None
    return army.location_id


def _create_message(
    session: Session,
    *,
    sender_name: str,
    sender_commander_id: int | None,
    sender_stronghold_id: int | None,
    recipient_id: int,
    origin_h3: str,
    destination_h3: str,
    content: str,
    priority: str,
    sent_day: int,
    sent_watch: int,
) -> Message:
    travel_watches = _message_travel_watches(origin_h3, destination_h3)
    delivery_day, delivery_watch = _advance_day_watch(sent_day, sent_watch, travel_watches)
    message = Message(
        sender_name=sender_name,
        sender_commander_id=sender_commander_id,
        sender_stronghold_id=sender_stronghold_id,
        recipient_id=recipient_id,
        content=content,
        priority=priority,
        sent_day=sent_day,
        sent_watch=sent_watch,
        delivery_day=delivery_day,
        delivery_watch=delivery_watch,
        status="in_transit",
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(message)
    return message


def _process_messages_tick(session: Session, clock: GameClock) -> dict[str, int]:
    due_messages = (
        session.query(Message)
        .filter(Message.status == "in_transit", _is_delivered_filter(clock.day, clock.watch))
        .all()
    )
    received = 0
    lost = 0
    for message in due_messages:
        if random.random() < MESSAGE_LOSS_PROBABILITY:
            message.status = "lost"
            lost += 1
        else:
            message.status = "received"
            received += 1
    return {"received": received, "lost": lost}


def _advance_day_watch(day: int, watch: int, steps: int = 1) -> tuple[int, int]:
    current_day = day
    current_watch = watch
    for _ in range(steps):
        next_watch = (current_watch + 1) % 5
        if current_watch == int(Watch.NIGHT) and next_watch == int(Watch.MATIN):
            current_day += 1
        current_watch = next_watch
    return current_day, current_watch


def _advance_active_watches(day: int, watch: int, steps: int) -> tuple[int, int]:
    """Advance by non-night watches only; night transitions do not consume progress."""
    current_day = day
    current_watch = watch
    remaining = steps
    while remaining > 0:
        current_day, current_watch = _advance_day_watch(current_day, current_watch, 1)
        if current_watch != int(Watch.NIGHT):
            remaining -= 1
    return current_day, current_watch


def _watch_is_at_or_after(day: int, watch: int, other_day: int, other_watch: int) -> bool:
    return (day, watch) >= (other_day, other_watch)


def _scenario_date_for_day(day: int) -> date:
    return SCENARIO_EPOCH + timedelta(days=max(day - 1, 0))


def _get_destination_h3(action: Action) -> str | None:
    try:
        payload = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        return None
    destination_h3 = payload.get("destination_h3")
    if isinstance(destination_h3, str) and destination_h3:
        return destination_h3
    return None


def _forage_supply_gain_for_army(session: Session, army: Army) -> tuple[int, list[Location]]:
    radius = _environs_radius_for_army(army)
    visible_h3 = list(h3.grid_disk(army.location_id, radius))
    locations = session.query(Location).filter(Location.location_id.in_(visible_h3)).all()
    gain = sum(max(int(location.settlement or 0), 0) * 2500 for location in locations)
    return gain, locations


def _start_action_now_if_valid(session: Session, action: Action, army: Army, clock: GameClock) -> bool:
    if action.kind == "move":
        if clock.watch == int(Watch.NIGHT):
            # No movement starts at night; keep queued for next active watch.
            return False
        destination_h3 = _get_destination_h3(action)
        if destination_h3 is None:
            action.state = "failed"
            return False
        try:
            watches_needed = calculate_move_watches(session, army.army_id, destination_h3)
        except ValueError:
            action.state = "failed"
            return False
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        action.eta_day, action.eta_watch = _advance_active_watches(clock.day, clock.watch, watches_needed)
        return True

    if action.kind == "forage":
        if clock.watch != int(Watch.NIGHT):
            # Forage starts only at night; keep queued until watch 0.
            return False
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        # Forage duration is exactly 4 watches, ending at watch 4 the same day.
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, clock.watch, 4)
        return True

    action.state = "failed"
    return False


def _execute_action_tick(session: Session, clock: GameClock) -> dict[str, int]:
    started = 0
    completed = 0
    failed = 0

    active_actions = session.query(Action).filter(Action.state.in_(ACTIVE_ACTION_STATES)).all()
    in_progress_by_commander: dict[int, list[Action]] = defaultdict(list)
    queued_by_commander: dict[int, list[Action]] = defaultdict(list)
    for action in active_actions:
        if action.state == "in_progress":
            in_progress_by_commander[action.commander_id].append(action)
        elif action.state == "queued":
            queued_by_commander[action.commander_id].append(action)

    # Safety invariant: keep only one in-progress action per commander.
    for commander_id, commander_actions in in_progress_by_commander.items():
        commander_actions.sort(key=lambda a: (a.accepted_at, a.action_id))
        for extra in commander_actions[1:]:
            extra.state = "failed"
            failed += 1
        in_progress_by_commander[commander_id] = commander_actions[:1]

    # First, attempt to complete currently in-progress actions.
    for commander_id, commander_actions in in_progress_by_commander.items():
        action = commander_actions[0]
        army = session.query(Army).filter(Army.commander_id == action.commander_id).first()
        if army is None:
            action.state = "failed"
            failed += 1
            continue
        if action.eta_day is None or action.eta_watch is None:
            action.state = "failed"
            failed += 1
            continue
        if action.kind == "move" and clock.watch == int(Watch.NIGHT):
            continue
        if _watch_is_at_or_after(clock.day, clock.watch, action.eta_day, action.eta_watch):
            if action.kind == "move":
                destination_h3 = _get_destination_h3(action)
                if destination_h3 is None:
                    action.state = "failed"
                    failed += 1
                    continue
                if destination_h3 not in set(list_valid_destinations(session, army.army_id)):
                    action.state = "failed"
                    failed += 1
                    continue
                destination_location = session.get(Location, destination_h3)
                if destination_location is None:
                    action.state = "failed"
                    failed += 1
                    continue
                # Keep FK and relationship state consistent for follow-on queued actions in the same tick.
                army.location = destination_location
                session.add(
                    Movement(
                        army_id=army.army_id,
                        location_id=destination_h3,
                        date=_scenario_date_for_day(clock.day),
                        watch=clock.watch,
                    )
                )
                action.state = "completed"
                completed += 1
                continue

            if action.kind == "forage":
                gain, visible_locations = _forage_supply_gain_for_army(session, army)
                capacity = supply_stats(army).capacity
                army.army_supply = min(capacity, army.army_supply + gain)
                for location in visible_locations:
                    if int(location.settlement or 0) > 0:
                        location.foraged_this_season = True
                action.state = "completed"
                completed += 1
                continue

            action.state = "failed"
            failed += 1

    # Then, promote queued actions when no in-progress action remains.
    commander_ids = set(in_progress_by_commander.keys()) | set(queued_by_commander.keys())
    for commander_id in commander_ids:
        has_in_progress = any(
            action.state == "in_progress" for action in in_progress_by_commander.get(commander_id, [])
        )
        if has_in_progress:
            continue
        queued = queued_by_commander.get(commander_id, [])
        queued.sort(key=lambda a: (a.accepted_at, a.action_id))
        army = session.query(Army).filter(Army.commander_id == commander_id).first()
        if army is None:
            for action in queued:
                action.state = "failed"
                failed += 1
            continue

        for action in queued:
            if not _start_action_now_if_valid(session, action, army, clock):
                if action.state == "queued":
                    # Night: leave queued; do not advance to next queued action.
                    break
                failed += 1
                continue
            started += 1
            break

    return {"started": started, "completed": completed, "failed": failed}


def _get_current_commander_id(
    authorization: str = Header(default=""),
    session: Session = Depends(_get_session),
) -> int:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = token.strip().strip("\"")
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    auth_token = session.get(AuthToken, token)
    if auth_token is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return auth_token.commander_id


def _find_commander_army(session: Session, commander_id: int) -> Army:
    army = session.query(Army).filter(Army.commander_id == commander_id).first()
    if army is None:
        raise HTTPException(status_code=404, detail="No army found for commander")
    return army


def _serialize_army(army: Army) -> dict[str, Any]:
    stats = supply_stats(army)

    status_flags = []
    if army.is_embarked:
        status_flags.append("embarked")
    if army.is_garrison:
        status_flags.append("garrison")
    if not status_flags:
        status_flags.append("marching")

    return {
        "army_id": _army_ref(army.army_id),
        "name": army.army_name,
        "location": {"h3": army.location_id},
        "composition": {
            "detachments": [
                {
                    "id": _detachment_ref(det.detachment_id),
                    "name": det.detachment_name,
                    "warriors": det.warrior_count,
                    "wagons": det.wagon_count,
                    "is_cavalry": det.is_cavalry,
                }
                for det in army.detachments
            ],
            "noncombatants": army.noncombattant_count,
        },
        "supply": {
            "current": army.army_supply,
            "capacity": stats.capacity,
            "daily_consumption": stats.daily_consumption,
            "days_estimate": stats.days_estimate,
        },
        "status_flags": status_flags,
    }


def _serialize_environs(
    session: Session,
    center_h3: str,
    radius: int,
    exclude_army_id: int | None = None,
) -> dict[str, Any]:
    disk = list(h3.grid_disk(center_h3, radius))
    locations = session.query(Location).filter(Location.location_id.in_(disk)).all()

    terrain_ids = {loc.terrain_id for loc in locations}
    terrains = {
        terrain.terrain_id: terrain
        for terrain in session.query(TerrainType).filter(TerrainType.terrain_id.in_(terrain_ids)).all()
    }
    strongholds = {
        sh.location_id: sh
        for sh in session.query(Stronghold).filter(Stronghold.location_id.in_(disk)).all()
    }
    region_names = {loc.region for loc in locations if loc.region}
    region_control_by_name = {}
    if region_names:
        region_control_by_name = {
            sh.stronghold_name: sh.control
            for sh in session.query(Stronghold).filter(Stronghold.stronghold_name.in_(region_names)).all()
        }
    other_armies_by_location: dict[str, list[dict[str, Any]]] = {}
    other_armies_query = (
        session.query(Army)
        .options(joinedload(Army.detachments), joinedload(Army.commander))
        .filter(Army.location_id.in_(disk), Army.is_garrison.is_(False))
    )
    if exclude_army_id is not None:
        other_armies_query = other_armies_query.filter(Army.army_id != exclude_army_id)
    for other_army in other_armies_query.all():
        location_bucket = other_armies_by_location.setdefault(other_army.location_id, [])
        infantry = sum(det.warrior_count for det in other_army.detachments if not det.is_cavalry)
        cavalry = sum(det.warrior_count for det in other_army.detachments if det.is_cavalry)
        total_strength = infantry + cavalry
        distance = max(0, _grid_distance(center_h3, other_army.location_id))
        intel: dict[str, Any] = {
            "faction": other_army.army_faction,
            "distance_cells": distance,
        }
        if distance <= 1:
            intel.update(
                {
                    "name": other_army.army_name,
                    "commander": (
                        other_army.commander.commander_name
                        if other_army.commander is not None
                        else None
                    ),
                    "infantry": infantry,
                    "cavalry": cavalry,
                }
            )
        elif distance <= 3:
            intel["strength_rounded"] = int(((total_strength + 500) // 1000) * 1000)
        location_bucket.append(
            {
                "army_id": _army_ref(other_army.army_id),
                **intel,
            }
        )

    cells = []
    for location in locations:
        terrain = terrains.get(location.terrain_id)
        stronghold = strongholds.get(location.location_id)
        other_armies = other_armies_by_location.get(location.location_id, [])
        cells.append(
            {
                "h3": location.location_id,
                "terrain_type": terrain.terrain_name if terrain else "unknown",
                "has_road": location.is_road,
                "region": location.region,
                "region_control": region_control_by_name.get(location.region) if location.region else None,
                "settlement": location.settlement,
                "foraged_this_season": location.foraged_this_season,
                "stronghold": (
                    {
                        "id": _stronghold_ref(stronghold.stronghold_id),
                        "name": stronghold.stronghold_name,
                        "type": stronghold.stronghold_type,
                        "faction": stronghold.control,
                    }
                    if stronghold
                    else None
                ),
                "observations": [],
                "other_armies": other_armies,
            }
        )

    cells.sort(key=lambda c: c["h3"])
    return {
        "center_h3": center_h3,
        "radius": radius,
        "cells": cells,
    }


def _serialize_message_summary(messages: list[Message]) -> dict[str, Any]:
    unread_count = sum(1 for message in messages if not message.is_read)
    latest = []
    for message in messages[:10]:
        latest.append(
            {
                "id": _message_ref(message.message_id),
                "from": {"name": _message_sender_display_name(message)},
                "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
                "is_read": message.is_read,
            }
        )

    return {"unread_count": unread_count, "latest": latest}


def _serialize_action(action: Action) -> dict[str, Any]:
    payload = {
        "action_id": _action_ref(action.action_id),
        "kind": action.kind,
        "state": action.state,
        "eta": None,
    }
    if action.eta_day is not None and action.eta_watch is not None:
        payload["eta"] = _to_watch_stamp(action.eta_day, action.eta_watch)
    return payload


def _environs_radius_for_army(army: Army) -> int:
    return 4 if any(detachment.is_cavalry for detachment in army.detachments) else 2


def _get_current_action_row(session: Session, commander_id: int) -> Action | None:
    in_progress = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state == "in_progress")
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .first()
    )
    if in_progress is not None:
        return in_progress
    return (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state == "queued")
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .first()
    )


@router.post("/auth/login")
def login(payload: LoginRequest, session: Session = Depends(_get_session)):
    commander = (
        session.query(Commander)
        .filter(Commander.commander_name.ilike(payload.commander_name.strip()))
        .first()
    )
    if commander is None:
        raise HTTPException(status_code=404, detail="Commander not found")

    token = secrets.token_urlsafe(24)
    session.add(
        AuthToken(
            token=token,
            commander_id=commander.commander_id,
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    return {
        "token": token,
        "commander": {
            "id": _commander_ref(commander.commander_id),
            "name": commander.commander_name,
        },
    }


@router.get("/commanders")
def list_commanders(session: Session = Depends(_get_session)):
    commanders = session.query(Commander).order_by(Commander.commander_name.asc()).all()
    return [
        {
            "id": _commander_ref(commander.commander_id),
            "name": commander.commander_name,
            "title": commander.commander_title,
            "display_name": _commander_display_name(commander),
        }
        for commander in commanders
    ]


@router.get("/time")
def get_time(session: Session = Depends(_get_session)):
    return _clock_payload(_get_or_create_clock(session))


@router.post("/admin/time/advance")
def advance_time_for_development(
    payload: TimeAdvanceRequest,
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
):
    if payload.steps < 1:
        raise HTTPException(status_code=400, detail="steps must be >= 1")

    configured_admin_token = os.getenv("DEV_ADMIN_TOKEN")
    if configured_admin_token and x_admin_token != configured_admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")

    clock = _get_or_create_clock(session)
    start = _clock_payload(clock)
    timeline = []
    actions_started = 0
    actions_completed = 0
    actions_failed = 0

    for _ in range(payload.steps):
        clock.day, clock.watch = _advance_day_watch(clock.day, clock.watch, 1)
        supply_result = None
        if clock.watch == int(Watch.NIGHT):
            supply_result = consume_supply_for_all_armies(session)
        message_result = _process_messages_tick(session, clock)
        tick_result = {"started": 0, "completed": 0, "failed": 0}
        if payload.execute_actions:
            tick_result = _execute_action_tick(session, clock)
            actions_started += tick_result["started"]
            actions_completed += tick_result["completed"]
            actions_failed += tick_result["failed"]
        timeline.append(
            {
                "time": _clock_payload(clock),
                "actions": tick_result,
                "supply": supply_result,
                "messages": {
                    "generated": 0,
                    "received": message_result["received"],
                    "lost": message_result["lost"],
                },
            }
        )

    session.commit()
    return {
        "start_time": start,
        "end_time": _clock_payload(clock),
        "steps": payload.steps,
        "execute_actions": payload.execute_actions,
        "timeline": timeline,
        "actions_summary": {
            "started": actions_started,
            "completed": actions_completed,
            "failed": actions_failed,
        },
    }


@router.get("/me/view")
def get_my_view(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    clock = _get_or_create_clock(session)
    army = _find_commander_army(session, commander_id)
    environs_radius = _environs_radius_for_army(army)

    delivered_messages = (
        session.query(Message)
        .filter(
            Message.recipient_id == commander_id,
            Message.status == "received",
            _is_delivered_filter(clock.day, clock.watch),
        )
        .order_by(Message.delivery_day.desc(), Message.delivery_watch.desc(), Message.message_id.desc())
        .all()
    )

    current_action = _get_current_action_row(session, commander_id)

    return {
        "time": _clock_payload(clock),
        "army": _serialize_army(army),
        "environs": _serialize_environs(
            session,
            army.location_id,
            environs_radius,
            exclude_army_id=army.army_id,
        ),
        "messages": _serialize_message_summary(delivered_messages),
        "current_action": _serialize_action(current_action) if current_action else None,
    }


@router.get("/me/roads/border")
def get_border_road_neighbors(
    cells: str = Query(..., description="Comma-separated H3 cells currently visible"),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    _ = commander_id  # endpoint is still commander-scoped via auth
    requested = [value.strip() for value in cells.split(",") if value.strip()]
    visible_set = set(requested)
    if not visible_set:
        return {"roads": []}

    h3_module = h3
    neighbor_candidates: set[str] = set()
    for cell in visible_set:
        try:
            neighbors = set(h3_module.grid_ring(cell, 1))
        except Exception:
            continue
        neighbor_candidates.update(neighbors - visible_set)

    if not neighbor_candidates:
        return {"roads": []}

    road_neighbors = (
        session.query(Location.location_id)
        .filter(Location.location_id.in_(neighbor_candidates), Location.is_road.is_(True))
        .all()
    )
    return {"roads": [row[0] for row in road_neighbors]}


@router.post("/me/actions")
def create_action(
    payload: ActionCreateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    clock = _get_or_create_clock(session)
    action_params: dict[str, Any] = {}
    if payload.kind == "move":
        destination_h3 = payload.destination_h3
        if not destination_h3:
            raise HTTPException(status_code=400, detail="destination_h3 is required for move actions")
        destination = session.get(Location, destination_h3)
        if destination is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Unknown move destination_h3",
                    "destination_h3": destination_h3,
                },
            )
        action_params["destination_h3"] = destination_h3
    elif payload.kind == "forage":
        # Forage can only be issued at Night (watch 0).
        if clock.watch != int(Watch.NIGHT):
            action = Action(
                commander_id=commander_id,
                kind=payload.kind,
                state="failed",
                parameters_json=json.dumps(action_params),
                accepted_at=datetime.now(timezone.utc),
            )
            session.add(action)
            session.commit()
            session.refresh(action)
            return {
                "action_id": _action_ref(action.action_id),
                "kind": action.kind,
                "state": action.state,
                "accepted_at": action.accepted_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }

        # Night forage preempts all existing active actions for this commander.
        active_actions = (
            session.query(Action)
            .filter(
                Action.commander_id == commander_id,
                Action.state.in_(ACTIVE_ACTION_STATES),
            )
            .all()
        )
        for existing in active_actions:
            existing.state = "cancelled"

    action = Action(
        commander_id=commander_id,
        kind=payload.kind,
        state="queued",
        parameters_json=json.dumps(action_params),
        accepted_at=datetime.now(timezone.utc),
    )
    session.add(action)

    # Immediate start: if commander has no in-progress action, this action becomes active now.
    if payload.kind == "forage":
        if not _start_action_now_if_valid(session, action, army, clock):
            action.state = "failed"
    else:
        in_progress_exists = (
            session.query(Action)
            .filter(
                Action.commander_id == commander_id,
                Action.state == "in_progress",
            )
            .first()
            is not None
        )
        if not in_progress_exists:
            _start_action_now_if_valid(session, action, army, clock)

    session.commit()
    session.refresh(action)

    return {
        "action_id": _action_ref(action.action_id),
        "kind": action.kind,
        "state": action.state,
        "accepted_at": action.accepted_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


@router.get("/correspondents")
def list_correspondents(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    correspondents = (
        session.query(Commander)
        .filter(Commander.commander_id != commander_id)
        .order_by(Commander.commander_name.asc())
        .all()
    )
    return [
        {
            "id": _commander_ref(commander.commander_id),
            "name": commander.commander_name,
            "title": commander.commander_title,
            "display_name": _commander_display_name(commander),
        }
        for commander in correspondents
    ]


@router.get("/me/actions/current")
def get_current_action(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    current = _get_current_action_row(session, commander_id)
    if current is None:
        return None
    return _serialize_action(current)


@router.post("/me/actions/{action_id}/cancel")
def cancel_action(
    action_id: str,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    action_pk = _parse_action_ref(action_id)
    action = session.get(Action, action_pk)
    if action is None or action.commander_id != commander_id:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.state not in ACTIVE_ACTION_STATES:
        raise HTTPException(status_code=409, detail="Action cannot be cancelled in current state")

    action.state = "cancelled"
    session.commit()
    return {
        "action_id": _action_ref(action.action_id),
        "state": action.state,
    }


@router.post("/me/messages")
def send_message(
    payload: MessageCreateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    sender = session.get(Commander, commander_id)
    if sender is None:
        raise HTTPException(status_code=404, detail="Sender commander not found")

    recipient_id = _parse_commander_ref(payload.recipient_id)
    recipient = session.get(Commander, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")

    clock = _get_or_create_clock(session)
    sender_h3 = _commander_location_h3(session, commander_id)
    recipient_h3 = _commander_location_h3(session, recipient_id)
    if sender_h3 is None or recipient_h3 is None:
        raise HTTPException(status_code=422, detail="Sender or recipient has no mappable army location")

    message = _create_message(
        session,
        sender_name=_commander_display_name(sender),
        sender_commander_id=commander_id,
        sender_stronghold_id=None,
        recipient_id=recipient_id,
        origin_h3=sender_h3,
        destination_h3=recipient_h3,
        content=payload.content,
        priority=payload.priority,
        sent_day=clock.day,
        sent_watch=clock.watch,
    )
    session.commit()
    session.refresh(message)

    sent_watch = _to_watch_stamp(message.sent_day, message.sent_watch)
    return {
        "message_id": _message_ref(message.message_id),
        "sent_watch": sent_watch,
        "estimated_delivery_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
        "status": message.status,
    }


@router.get("/me/messages")
def list_messages(
    unread_only: bool = Query(default=False),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    clock = _get_or_create_clock(session)
    query = session.query(Message).filter(
        Message.recipient_id == commander_id,
        Message.status == "received",
        _is_delivered_filter(clock.day, clock.watch),
    )
    query = query.options(joinedload(Message.sender_commander))
    if unread_only:
        query = query.filter(Message.is_read.is_(False))

    messages = query.order_by(Message.delivery_day.desc(), Message.delivery_watch.desc(), Message.message_id.desc()).all()

    response = []
    for message in messages:
        response.append(
            {
                "id": _message_ref(message.message_id),
                "from": {"name": _message_sender_display_name(message)},
                "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
                "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
                "is_read": message.is_read,
            }
        )
    return response


@router.get("/me/messages/{message_id}")
def get_message(
    message_id: str,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    message_pk = _parse_message_ref(message_id)
    message = session.get(Message, message_pk)
    if message is None or message.recipient_id != commander_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.status != "received":
        if message.status == "lost":
            raise HTTPException(status_code=404, detail="Message was lost in transit")
        raise HTTPException(status_code=404, detail="Message not delivered yet")

    clock = _get_or_create_clock(session)
    if (message.delivery_day > clock.day) or (
        message.delivery_day == clock.day and message.delivery_watch > clock.watch
    ):
        raise HTTPException(status_code=404, detail="Message not delivered yet")

    if not message.is_read:
        message.is_read = True
        session.commit()

    return {
        "id": _message_ref(message.message_id),
        "from": {"name": _message_sender_display_name(message)},
        "content": message.content,
        "priority": message.priority,
        "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
        "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
        "is_read": message.is_read,
    }
