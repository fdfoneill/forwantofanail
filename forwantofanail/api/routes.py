from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
from typing import Any

import h3
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from forwantofanail.api.schemas import ActionCreateRequest, LoginRequest, MessageCreateRequest
from forwantofanail.core.database import create_session
from forwantofanail.core.models import (
    Action,
    Army,
    AuthToken,
    Commander,
    GameClock,
    Location,
    Message,
    Stronghold,
    TerrainType,
)
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
    total_people = army.noncombattant_count + sum(det.warrior_count for det in army.detachments)
    days_estimate = round(army.army_supply / total_people, 2) if total_people > 0 else None

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
            "capacity": None,
            "days_estimate": days_estimate,
        },
        "status_flags": status_flags,
    }


def _serialize_environs(session: Session, center_h3: str, radius: int) -> dict[str, Any]:
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

    cells = []
    for location in locations:
        terrain = terrains.get(location.terrain_id)
        stronghold = strongholds.get(location.location_id)
        cells.append(
            {
                "h3": location.location_id,
                "terrain_type": terrain.terrain_name if terrain else "unknown",
                "has_road": location.is_road,
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
                "from": {"name": message.sender.commander_name},
                "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
                "snippet": message.content[:120],
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
    return (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
        .order_by(Action.accepted_at.desc())
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


@router.get("/time")
def get_time(session: Session = Depends(_get_session)):
    return _clock_payload(_get_or_create_clock(session))


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
        .filter(Message.recipient_id == commander_id, _is_delivered_filter(clock.day, clock.watch))
        .order_by(Message.delivery_day.desc(), Message.delivery_watch.desc(), Message.message_id.desc())
        .all()
    )

    current_action = _get_current_action_row(session, commander_id)

    return {
        "time": _clock_payload(clock),
        "army": _serialize_army(army),
        "environs": _serialize_environs(session, army.location_id, environs_radius),
        "messages": _serialize_message_summary(delivered_messages),
        "current_action": _serialize_action(current_action) if current_action else None,
    }


@router.post("/me/actions")
def create_action(
    payload: ActionCreateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)

    existing = _get_current_action_row(session, commander_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Commander already has an active action")

    destination_h3 = payload.destination_h3
    destination = session.get(Location, destination_h3)
    if destination is None:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Unknown move destination_h3",
                "destination_h3": destination_h3,
            },
        )

    action = Action(
        commander_id=commander_id,
        kind=payload.kind,
        state="queued",
        parameters_json=json.dumps({"destination_h3": destination_h3}),
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
    return [{"id": _commander_ref(commander.commander_id), "name": commander.commander_name} for commander in correspondents]


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
    recipient_id = _parse_commander_ref(payload.recipient_id)
    recipient = session.get(Commander, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")

    clock = _get_or_create_clock(session)
    message = Message(
        sender_id=commander_id,
        recipient_id=recipient_id,
        content=payload.content,
        priority=payload.priority,
        sent_day=clock.day,
        sent_watch=clock.watch,
        delivery_day=clock.day,
        delivery_watch=clock.watch,
        status="delivered",
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(message)
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
        _is_delivered_filter(clock.day, clock.watch),
    )
    if unread_only:
        query = query.filter(Message.is_read.is_(False))

    messages = query.order_by(Message.delivery_day.desc(), Message.delivery_watch.desc(), Message.message_id.desc()).all()

    response = []
    for message in messages:
        response.append(
            {
                "id": _message_ref(message.message_id),
                "from": {"name": message.sender.commander_name},
                "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
                "snippet": message.content[:120],
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
        "from": {"name": message.sender.commander_name},
        "content": message.content,
        "priority": message.priority,
        "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
        "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
        "is_read": message.is_read,
    }
