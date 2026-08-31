from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import hmac
import base64
import math
import os
import json
from pathlib import Path
import random
import secrets
import threading
from typing import Any

import h3
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import and_, case, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload

from forwantofanail.api.schemas import (
    ActionCreateRequest,
    AlertIdsRequest,
    ArmyManagementApplyRequest,
    ActionPlanRequest,
    ClaimRequest,
    LoginRequest,
    MessageCreateRequest,
    StandingFollowRoadUpdateRequest,
    TimeAdvanceRequest,
)
from forwantofanail.core.database import create_session
from forwantofanail.core.models import (
    Action,
    Alert,
    AlertRecipient,
    Army,
    AuthToken,
    Commander,
    CommanderClaim,
    Detachment,
    GameClock,
    IdempotencyRecord,
    Location,
    Message,
    Movement,
    Siege,
    SiegeParticipant,
    StandingOrder,
    Stronghold,
    TerrainType,
)
from forwantofanail.core.scenario_catalog import ScenarioCatalogError, load_historical_stronghold_catalog
from forwantofanail.mechanics.forage import (
    forage_condition_word as _forage_condition_word,
    forage_depletion_level,
)
from forwantofanail.mechanics.location_description import (
    build_commander_brief,
    describe_army_location,
    describe_march_stage,
    describe_route_endpoint,
)
from forwantofanail.mechanics.movement import (
    calculate_move_watches,
    calculate_move_watches_from_origin,
    list_valid_destinations,
    list_valid_destinations_from_origin,
)
from forwantofanail.mechanics.navigation import RouteNotFoundError, build_route_summary
from forwantofanail.mechanics.supply import (
    consume_supply_for_all_armies,
    noncombatant_count,
    supply_stats,
)
from forwantofanail.mechanics.time import Watch, from_world_tick, to_world_tick
from forwantofanail.history.snapshots import (
    army_history_payload,
    capture_world_snapshot,
    record_history_event,
)

router = APIRouter(prefix="/v1")
ARMY_MANAGEMENT_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "data" / "army_management_templates.json"
COMMANDER_OVERVIEWS_PATH = Path(__file__).resolve().parents[1] / "data" / "commander_overviews.json"
FACTION_OVERVIEWS_PATH = Path(__file__).resolve().parents[1] / "data" / "faction_overviews.json"
COMMANDER_PORTRAIT_DIR = Path(__file__).resolve().parents[1] / "data" / "assets"
SESSION_COOKIE_NAME = "fwoan_session"
_SQLITE_MUTATION_LOCK = threading.RLock()

WATCH_LABELS = {
    Watch.NIGHT: "night",
    Watch.MATIN: "matin",
    Watch.PRIME: "prime",
    Watch.NOON: "sixbell",
    Watch.VESPER: "vesper",
}
ACTIVE_ACTION_STATES = {"queued", "in_progress"}
SCENARIO_EPOCH = date(1410, 5, 20)
MESSAGE_LOSS_PROBABILITY = 0.0
MAX_FOLLOW_ROAD_STEPS = 4
ALERT_TYPES = {"world event", "action", "report", "violence", "morale"}
BATTLE_ALERT_IMPORTANCE = "high"
SIEGE_RESISTANCE_BY_TYPE = {
    "town": 10.0,
    "city": 15.0,
    "fortress": 20.0,
}
SIEGE_DEFENDER_BONUS_BY_TYPE = {
    "town": 3,
    "city": 4,
    "fortress": 5,
}
SIEGE_LOOT_SCALE_BY_TYPE = {
    "town": 10000,
    "city": 100000,
    "fortress": 1000,
}
SIEGE_NONCOMBATANT_GAIN_BY_TYPE = {
    "town": 0.10,
    "city": 0.15,
    "fortress": 0.05,
}
WATCH_CHRONOLOGICAL_SORT = {
    int(Watch.MATIN): 0,
    int(Watch.PRIME): 1,
    int(Watch.NOON): 2,
    int(Watch.VESPER): 3,
    int(Watch.NIGHT): 4,
}


def _watch_chronological_order_sql(column):
    return case(WATCH_CHRONOLOGICAL_SORT, value=column, else_=-1)


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


def _parse_army_ref(value: str) -> int:
    if value.startswith("army_"):
        value = value[5:]
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="target_army_id must be an integer or army_<id>") from exc


def _parse_stronghold_ref(value: str) -> int:
    if value.startswith("sh_"):
        value = value[3:]
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="target_stronghold_id must be an integer or sh_<id>") from exc


def _parse_detachment_ref(value: str) -> int:
    if value.startswith("det_"):
        value = value[4:]
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="detachment_id must be an integer or det_<id>") from exc


def _commander_display_name(commander: Commander) -> str:
    title = (commander.commander_title or "").strip()
    name = (commander.commander_name or "").strip()
    return f"{title} {name}".strip() if title else name


def _message_sender_display_name(message: Message) -> str:
    # sender_name is an immutable snapshot; historical correspondence must not
    # change when a commander is renamed later.
    return message.sender_name


def _message_recipient_display_name(message: Message) -> str:
    if message.recipient is None:
        return "Unknown recipient"
    return _commander_display_name(message.recipient)


def _faction_portrait_filename(faction: str) -> str | None:
    faction_slug = "_".join(
        part for part in "".join(ch.lower() if ch.isalnum() else " " for ch in str(faction or "")).split() if part
    )
    if not faction_slug:
        return None
    filename = f"portrait_{faction_slug}.png"
    return filename if (COMMANDER_PORTRAIT_DIR / filename).is_file() else None


def _commander_portrait_filename(commander: Commander, faction: str = "") -> str | None:
    display_name = _commander_display_name(commander)
    normalized_display = "".join(ch for ch in display_name.lower() if ch.isalnum())
    for path in COMMANDER_PORTRAIT_DIR.glob("Portrait - *"):
        if not path.is_file():
            continue
        stem = path.stem.removeprefix("Portrait - ")
        normalized_stem = "".join(ch for ch in stem.lower() if ch.isalnum())
        if normalized_stem == normalized_display or normalized_display.startswith(normalized_stem):
            return path.name
        commander_name = "".join(ch for ch in str(commander.commander_name or "").lower() if ch.isalnum())
        if commander_name and commander_name.startswith(normalized_stem):
            return path.name
        if normalized_stem and normalized_stem in commander_name:
            return path.name
    return _faction_portrait_filename(faction)


def _commander_portrait_url(commander: Commander, faction: str = "") -> str | None:
    filename = _commander_portrait_filename(commander, faction)
    if filename is None:
        return None
    return f"/v1/commander-portraits/{filename}"


def _commander_claim_faction(session: Session, commander_id: int) -> str:
    army = (
        session.query(Army)
        .filter(Army.commander_id == commander_id, Army.is_garrison.is_(False))
        .order_by(Army.army_id.asc())
        .first()
    )
    return str(army.army_faction or "").strip() if army is not None else ""


def _load_overview_mapping(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key).strip(): str(value).strip() for key, value in raw.items() if str(key).strip() and str(value).strip()}


def _lookup_commander_overview(commander: Commander) -> str:
    overviews = _load_overview_mapping(COMMANDER_OVERVIEWS_PATH)
    keys = [
        _commander_ref(int(commander.commander_id)),
        _commander_display_name(commander),
        str(commander.commander_name or "").strip(),
    ]
    for key in keys:
        if key and key in overviews:
            return overviews[key]
    return ""


def _generated_commander_overview(session: Session, commander: Commander) -> str:
    source_id = getattr(commander, "created_by_commander_id", None)
    created_day = getattr(commander, "created_day", None)
    if source_id is None or created_day is None:
        return ""
    source = session.get(Commander, int(source_id))
    source_name = _commander_display_name(source) if source is not None else "an unknown commander"
    return f"Dispatched by {source_name} on {_scenario_date_for_day(int(created_day)).isoformat()}."


def _commander_overview_payload(session: Session, commander: Commander, faction: str) -> dict[str, str]:
    faction_overviews = _load_overview_mapping(FACTION_OVERVIEWS_PATH)
    faction_overview = faction_overviews.get(str(faction or "").strip(), "")
    commander_overview = _lookup_commander_overview(commander) or _generated_commander_overview(session, commander)
    parts = [part for part in [commander_overview, faction_overview] if part]
    return {
        "faction": faction_overview,
        "commander": commander_overview,
        "combined": "\n\n".join(parts),
    }


def _serialize_claim_commander(session: Session, commander: Commander, claim: CommanderClaim | None = None) -> dict[str, Any]:
    faction = _commander_claim_faction(session, int(commander.commander_id))
    return {
        "id": _commander_ref(commander.commander_id),
        "name": commander.commander_name,
        "title": commander.commander_title,
        "display_name": _commander_display_name(commander),
        "faction": faction,
        "is_original": commander.created_by_commander_id is None,
        "overview": _commander_overview_payload(session, commander, faction),
        "portrait_url": _commander_portrait_url(commander, faction),
        "claimed": claim is not None,
        "claimed_at": claim.claimed_at.replace(microsecond=0).isoformat().replace("+00:00", "Z") if claim else None,
    }


def _alert_ref(alert_id: int) -> str:
    return f"alt_{alert_id}"


def _parse_alert_ref(value: str) -> int:
    normalized = str(value or "").removeprefix("alt_")
    try:
        return int(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="alert_id must be an integer or alt_<id>") from exc


def _cell_army_display_name(army_info: dict[str, Any]) -> str:
    # Use the best available identifier from serialized /view intel.
    name = str(army_info.get("name") or "").strip()
    if name:
        return name
    commander = str(army_info.get("commander") or "").strip()
    if commander:
        return f"{commander}'s army"
    faction = str(army_info.get("faction") or "").strip()
    if faction:
        return f"{faction} army"
    return "unknown army"


def _cell_title(
    *,
    terrain_type: str,
    has_road: bool,
    stronghold_name: str | None,
    region_name: str | None,
    other_armies: list[dict[str, Any]],
) -> str:
    first_army = other_armies[0] if other_armies else None
    if first_army and stronghold_name:
        return f"{_cell_army_display_name(first_army)} occupying {stronghold_name}"
    if first_army:
        return _cell_army_display_name(first_army)
    if stronghold_name:
        return stronghold_name
    normalized_terrain = terrain_type.strip().lower()
    if normalized_terrain in {"river", "open water"} and has_road:
        return "bridge"
    if has_road:
        return "road"
    if normalized_terrain == "open water":
        region_name = str(region_name or "").strip()
        if region_name:
            return region_name
    return terrain_type


def _army_has_live_detachments(army: Army | None) -> bool:
    if army is None:
        return False
    return any(int(det.warrior_count or 0) > 0 for det in army.detachments)


def _live_warrior_count(army: Army | None) -> int:
    if army is None:
        return 0
    return sum(int(det.warrior_count or 0) for det in army.detachments if int(det.warrior_count or 0) > 0)


def _get_session():
    session = create_session()
    try:
        yield session
    finally:
        session.close()


def _is_database_busy_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message or "database table is locked" in message


def _run_world_mutation(session: Session, operation):
    def execute():
        try:
            result = operation()
            session.flush()
            session.commit()
            return result
        except HTTPException:
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            if _is_database_busy_error(exc):
                raise HTTPException(status_code=503, detail="Database is busy; retry the request.") from exc
            raise HTTPException(status_code=409, detail="Game state changed; refresh and try again.") from exc
        except OperationalError as exc:
            session.rollback()
            if _is_database_busy_error(exc) or "deadlock" in str(exc).lower() or "serialization" in str(exc).lower():
                raise HTTPException(status_code=503, detail="Database is busy; retry the request.") from exc
            raise
        except Exception:
            session.rollback()
            raise

    if session.bind is not None and session.bind.dialect.name == "sqlite":
        with _SQLITE_MUTATION_LOCK:
            return execute()
    return execute()


def _payload_dict(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if hasattr(payload, "dict"):
        return payload.dict()
    return payload


def _run_idempotent_mutation(
    session: Session,
    *,
    actor_scope: str,
    route: str,
    idempotency_key: str | None,
    payload: Any,
    operation,
):
    if idempotency_key is not None and idempotency_key.__class__.__name__ == "Header":
        idempotency_key = f"direct-{secrets.token_hex(16)}"
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    key = idempotency_key.strip()
    if len(key) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    encoded = json.dumps(_payload_dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def idempotent_operation():
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            lock_name = f"{actor_scope}:{route}:{key}"
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"), {"lock_name": lock_name})
        existing = (
            session.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.actor_scope == actor_scope,
                IdempotencyRecord.route == route,
                IdempotencyRecord.idempotency_key == key,
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with a different request")
            return json.loads(existing.response_json)
        result = operation()
        session.flush()
        session.add(
            IdempotencyRecord(
                actor_scope=actor_scope,
                route=route,
                idempotency_key=key,
                request_hash=request_hash,
                response_json=json.dumps(result, sort_keys=True, default=str),
                created_at=datetime.now(timezone.utc),
            )
        )
        return result

    return _run_world_mutation(session, idempotent_operation)


def _get_or_create_clock(session: Session, *, for_update: bool | str = False) -> GameClock:
    query = session.query(GameClock).filter(GameClock.singleton_id == 1)
    if for_update and session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(read=for_update != "exclusive")
    clock = query.one_or_none()
    if clock is None:
        clock = GameClock(singleton_id=1, day=1, watch=int(Watch.MATIN), world_tick=0)
        if for_update:
            session.add(clock)
    elif getattr(clock, "world_tick", None) is None or (
        int(clock.world_tick or 0) == 0 and (int(clock.day) != 1 or int(clock.watch) != int(Watch.MATIN))
    ):
        clock.world_tick = to_world_tick(int(clock.day), int(clock.watch))
    return clock


def _clock_payload(clock: GameClock) -> dict[str, int | str]:
    day, watch_enum = from_world_tick(int(clock.world_tick))
    clock.day = day
    clock.watch = int(watch_enum)
    return {
        "day": day,
        "calendar_date": _scenario_date_for_day(day).isoformat(),
        "watch": int(clock.watch),
        "watch_label": WATCH_LABELS[watch_enum],
    }


def _to_watch_stamp(day: int, watch: int) -> dict[str, int]:
    return {"day": day, "watch": watch}


def _is_delivered_filter(day: int, watch: int):
    return Message.delivery_tick <= to_world_tick(day, watch)


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
    sent_tick = to_world_tick(sent_day, sent_watch)
    delivery_tick = sent_tick + travel_watches
    delivery_day, delivery_watch_enum = from_world_tick(delivery_tick)
    delivery_watch = int(delivery_watch_enum)
    message = Message(
        sender_name=sender_name,
        sender_commander_id=sender_commander_id,
        sender_stronghold_id=sender_stronghold_id,
        recipient_id=recipient_id,
        content=content,
        priority=priority,
        sent_day=sent_day,
        sent_watch=sent_watch,
        sent_tick=sent_tick,
        delivery_day=delivery_day,
        delivery_watch=delivery_watch,
        delivery_tick=delivery_tick,
        status="in_transit",
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(message)
    return message


def _create_alert(
    session: Session,
    *,
    recipient_commander_id: int | None,
    alert_type: str,
    signal_kind: str = "event",
    message: str,
    created_day: int,
    created_watch: int,
    delivered_day: int | None = None,
    delivered_watch: int | None = None,
    category: str = "general",
    importance: str = "normal",
    payload: dict[str, Any] | None = None,
    event_key: str | None = None,
) -> Alert:
    normalized_type = alert_type.strip().lower()
    if normalized_type not in ALERT_TYPES:
        normalized_type = "report"
    normalized_message = str(message or "").strip()
    if normalized_type == "world event" and normalized_message and not normalized_message.startswith("NEWS:"):
        normalized_message = f"NEWS: {normalized_message}"
    normalized_signal_kind = signal_kind.strip().lower()
    if normalized_signal_kind not in {"event", "state"}:
        normalized_signal_kind = "event"
    created_tick = to_world_tick(created_day, created_watch)
    available_day = delivered_day if delivered_day is not None else created_day
    available_watch = delivered_watch if delivered_watch is not None else created_watch
    available_tick = to_world_tick(available_day, available_watch)
    alert = Alert(
        recipient_commander_id=recipient_commander_id,
        alert_type=normalized_type,
        signal_kind=normalized_signal_kind,
        category=(category or "general").strip().lower(),
        importance=(importance or "normal").strip().lower(),
        message=normalized_message,
        payload_json=json.dumps(payload or {}),
        created_day=created_day,
        created_watch=created_watch,
        created_tick=created_tick,
        delivered_day=available_day,
        delivered_watch=available_watch,
        available_tick=available_tick,
        is_read=False,
        event_key=event_key,
        created_at=datetime.now(timezone.utc),
    )
    session.add(alert)
    session.flush()
    if recipient_commander_id is None:
        recipient_ids = [row[0] for row in session.query(Commander.commander_id).all()]
    else:
        recipient_ids = [recipient_commander_id]
    for commander_id in recipient_ids:
        session.add(
            AlertRecipient(
                alert_id=alert.alert_id,
                commander_id=int(commander_id),
                available_tick=available_tick,
            )
        )
    return alert


def _serialize_alert(alert: Alert, receipt: AlertRecipient | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(alert.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": _alert_ref(alert.alert_id),
        "type": alert.alert_type,
        "signal_kind": alert.signal_kind,
        "category": alert.category,
        "importance": alert.importance,
        "message": alert.message,
        "created_watch": _to_watch_stamp(alert.created_day, alert.created_watch),
        "delivered_watch": _to_watch_stamp(alert.delivered_day, alert.delivered_watch),
        "is_read": bool(receipt.read_at) if receipt is not None else alert.is_read,
        "delivered_at": receipt.delivered_at.isoformat() if receipt is not None and receipt.delivered_at else None,
        "read_at": receipt.read_at.isoformat() if receipt is not None and receipt.read_at else None,
        "payload": payload,
    }


def _cancellation_narrative(cancelled_by_kind: dict[str, int]) -> str:
    parts: list[str] = []
    move_count = int(cancelled_by_kind.get("move", 0))
    forage_count = int(cancelled_by_kind.get("forage", 0))
    other_count = sum(
        int(count)
        for kind, count in cancelled_by_kind.items()
        if kind not in {"move", "forage"}
    )
    if move_count > 0:
        parts.append("planned march cancelled" if move_count == 1 else f"{move_count} planned marches cancelled")
    if forage_count > 0:
        parts.append("forage cancelled" if forage_count == 1 else f"{forage_count} forage actions cancelled")
    if other_count > 0:
        parts.append("active order cancelled" if other_count == 1 else f"{other_count} active orders cancelled")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{parts[0]}, {parts[1]}, and {parts[2]}"


def _apply_supply_loss(army: Army, percent: float) -> int:
    baseline = max(0, int(army.army_supply or 0))
    if baseline <= 0:
        return 0
    lost = max(0, int(round(baseline * max(0.0, percent))))
    lost = min(lost, baseline)
    army.army_supply = baseline - lost
    return lost


def _clamp_army_supply_to_capacity(army: Army) -> int:
    capacity = max(0, int(supply_stats(army).capacity or 0))
    current = max(0, int(army.army_supply or 0))
    clamped = min(current, capacity)
    army.army_supply = clamped
    return clamped


def _apply_random_warrior_loss(session: Session, army: Army, percent: float) -> int:
    detachments = [det for det in army.detachments if int(det.warrior_count or 0) > 0]
    if not detachments:
        return 0
    total = sum(int(det.warrior_count or 0) for det in detachments)
    if total <= 0:
        return 0
    target = min(total, max(0, int(round(total * max(0.0, percent)))))
    if target <= 0:
        return 0
    lost = 0
    available = list(detachments)
    while lost < target and available:
        det = random.choice(available)
        current = int(det.warrior_count or 0)
        if current <= 0:
            available.remove(det)
            continue
        det.warrior_count = current - 1
        lost += 1
        if det.warrior_count <= 0:
            available.remove(det)
            session.delete(det)
    return lost


def _remove_selected_detachments(session: Session, detachments: list[Any]) -> list[str]:
    removed_names: list[str] = []
    for det in detachments:
        removed_names.append(str(det.detachment_name or "Unnamed detachment"))
        session.delete(det)
    return removed_names


def _nearest_other_commander_army(session: Session, army: Army) -> Army | None:
    candidates = (
        session.query(Army)
        .filter(
            Army.army_id != army.army_id,
            Army.commander_id.is_not(None),
        )
        .all()
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda candidate: (
            max(0, _grid_distance(army.location_id, candidate.location_id)),
            int(candidate.army_id),
        ),
    )


def _run_morale_test_for_army(
    session: Session,
    *,
    army: Army,
    clock: GameClock,
    category: str,
) -> None:
    if army.is_garrison:
        return
    current_morale = _clamp_morale(army.army_morale)
    roll = random.randint(1, 6) + random.randint(1, 6)
    if roll <= current_morale:
        return

    message = "Grumbling from the troops..."
    payload: dict[str, Any] = {
        "roll": roll,
        "current_morale": current_morale,
    }

    if roll == 2:
        removed = [det for det in list(army.detachments) if random.random() < 0.5]
        removed_names = _remove_selected_detachments(session, removed)
        names_text = ", ".join(removed_names) if removed_names else "No detachments"
        message = f"Total mutiny! {names_text} have disbanded."
        payload["removed_detachments"] = removed_names
    elif roll == 3:
        lost_supply = _apply_supply_loss(army, 0.30)
        lost_warriors = _apply_random_warrior_loss(session, army, 0.30)
        message = f"Mass desertion! {lost_supply} supply and {lost_warriors} warriors lost."
        payload["lost_supply"] = lost_supply
        payload["lost_warriors"] = lost_warriors
    elif roll == 4:
        requested = random.randint(1, 6)
        detachments = list(army.detachments)
        random.shuffle(detachments)
        removed = detachments[: min(requested, len(detachments))]
        removed_names = _remove_selected_detachments(session, removed)
        names_text = ", ".join(removed_names) if removed_names else "No detachments"
        message = f"Major mutiny! {names_text} have disbanded."
        payload["removed_detachments"] = removed_names
    elif roll == 5:
        lost_supply = _apply_supply_loss(army, 0.20)
        lost_warriors = _apply_random_warrior_loss(session, army, 0.20)
        message = f"Major desertion! {lost_supply} supply and {lost_warriors} warriors lost."
        payload["lost_supply"] = lost_supply
        payload["lost_warriors"] = lost_warriors
    elif roll == 6:
        lost_supply = _apply_supply_loss(army, 0.20)
        message = f"Mass embezzlement! {lost_supply} supply stolen."
        payload["lost_supply"] = lost_supply
    elif roll == 7:
        detachments = list(army.detachments)
        if detachments:
            removed = random.choice(detachments)
            removed_name = str(removed.detachment_name or "Unnamed detachment")
            session.delete(removed)
            message = f"Mutiny! {removed_name} has disbanded."
            payload["removed_detachments"] = [removed_name]
        else:
            message = "Mutiny! But no detachments remained to disband."
    elif roll == 8:
        lost_supply = _apply_supply_loss(army, 0.10)
        lost_warriors = _apply_random_warrior_loss(session, army, 0.10)
        message = f"Desertion! {lost_supply} supply and {lost_warriors} warriors lost."
        payload["lost_supply"] = lost_supply
        payload["lost_warriors"] = lost_warriors
    elif roll == 9:
        recipient_army = _nearest_other_commander_army(session, army)
        recipient_name = "unknown recipient"
        if recipient_army is not None and recipient_army.commander_id is not None:
            recipient_commander = session.get(Commander, recipient_army.commander_id)
            if recipient_commander is not None:
                recipient_name = _commander_display_name(recipient_commander)
                descriptor = describe_army_location(session, army.location_id)
                _create_message(
                    session,
                    sender_name=f"A sympathizer in {army.army_name}",
                    sender_commander_id=None,
                    sender_stronghold_id=None,
                    recipient_id=recipient_commander.commander_id,
                    origin_h3=army.location_id,
                    destination_h3=recipient_army.location_id,
                    content=(
                        "I write from within the ranks. "
                        f"The army is currently {descriptor}."
                    ),
                    priority="urgent",
                    sent_day=clock.day,
                    sent_watch=clock.watch,
                )
        message = f"Treachery! A disgruntled officer has betrayed the army's position to {recipient_name}."
        payload["recipient"] = recipient_name
    elif roll == 10:
        current_pct = float(army.noncombattant_percent or 0.0)
        army.noncombattant_percent = max(0.0, current_pct + 0.05)
        new_noncombatants = noncombatant_count(army)
        message = f"New camp followers acquired. Army now has {new_noncombatants} noncombattants."
        payload["new_noncombatants"] = new_noncombatants
    elif roll == 11:
        lost_supply = _apply_supply_loss(army, 0.05)
        message = f"Thievery among the troops. {lost_supply} supply looted."
        payload["lost_supply"] = lost_supply

    _create_alert(
        session,
        recipient_commander_id=army.commander_id,
        alert_type="morale",
        signal_kind="event",
        category=category,
        importance="high",
        message=message,
        created_day=clock.day,
        created_watch=clock.watch,
        payload=payload,
    )


def _emit_no_supply_state_alerts(session: Session, clock: GameClock) -> None:
    armies = session.query(Army).filter(Army.commander_id.is_not(None)).all()
    for army in armies:
        if army.commander_id is None:
            continue
        if int(army.army_supply or 0) > 0:
            continue
        _create_alert(
            session,
            recipient_commander_id=army.commander_id,
            alert_type="report",
            signal_kind="state",
            category="supply",
            importance="high",
            message="No supplies: troops going hungry!",
            created_day=clock.day,
            created_watch=clock.watch,
        )


def _emit_supply_alerts_after_consumption(session: Session, clock: GameClock) -> None:
    armies = session.query(Army).filter(Army.commander_id.is_not(None)).all()
    for army in armies:
        if army.commander_id is None:
            continue
        stats = supply_stats(army)
        if army.army_supply <= 0:
            # Starvation erodes current morale before each resulting morale test.
            army.army_morale = _clamp_morale(int(army.army_morale or 0) - 1)
            _run_morale_test_for_army(
                session,
                army=army,
                clock=clock,
                category="supply",
            )
            continue
        days_estimate = stats.days_estimate
        if days_estimate is None:
            continue
        days_remaining = max(0, int(math.floor(days_estimate)))
        if days_remaining <= 7:
            _create_alert(
                session,
                recipient_commander_id=army.commander_id,
                alert_type="report",
                signal_kind="state",
                category="supply",
                importance="moderate",
                message=f"Supplies low: {days_remaining} days remaining",
                created_day=clock.day,
                created_watch=clock.watch,
            )


def _emit_enemy_proximity_alerts(session: Session, clock: GameClock) -> None:
    armies = session.query(Army).filter(Army.commander_id.is_not(None), Army.is_garrison.is_(False)).all()
    for army in armies:
        if army.commander_id is None:
            continue
        suppressed_enemy_army_ids: set[int] = set()
        occupied_stronghold = _stronghold_at_h3(session, army.location_id)
        if occupied_stronghold is not None:
            active_siege = _active_siege_for_stronghold(session, occupied_stronghold.stronghold_id)
            if active_siege is not None:
                besieger_faction = _active_siege_faction(session, active_siege)
                if besieger_faction and besieger_faction != army.army_faction:
                    defender_ids = {
                        defender.army_id
                        for defender in _defender_armies_in_stronghold(session, occupied_stronghold, besieger_faction)
                    }
                    if army.army_id in defender_ids:
                        suppressed_enemy_army_ids.update(
                            participant.besieger_army_id
                            for participant in _active_siege_participants_for_siege(session, active_siege)
                        )
                        _create_alert(
                            session,
                            recipient_commander_id=army.commander_id,
                            alert_type="report",
                            signal_kind="state",
                            category="contact",
                            importance="high",
                            message=f"Under siege by {besieger_faction} forces",
                            created_day=clock.day,
                            created_watch=clock.watch,
                        )
        radius = _environs_radius_for_army(army)
        disk = set(h3.grid_disk(army.location_id, radius))
        visible_enemies = (
            session.query(Army)
            .filter(
                Army.location_id.in_(disk),
                Army.army_id != army.army_id,
                Army.army_faction != army.army_faction,
                Army.is_garrison.is_(False),
            )
            .all()
        )
        nearest_by_faction: dict[str, int] = {}
        for enemy in visible_enemies:
            if enemy.army_id in suppressed_enemy_army_ids:
                continue
            distance = max(0, _grid_distance(army.location_id, enemy.location_id))
            prior = nearest_by_faction.get(enemy.army_faction)
            if prior is None or distance < prior:
                nearest_by_faction[enemy.army_faction] = distance
        for faction_name, distance in nearest_by_faction.items():
            _create_alert(
                session,
                recipient_commander_id=army.commander_id,
                alert_type="report",
                signal_kind="state",
                category="contact",
                importance="moderate",
                message=f"{faction_name} army {distance} leagues away",
                created_day=clock.day,
                created_watch=clock.watch,
            )


def _emit_rout_state_alerts(session: Session, clock: GameClock) -> None:
    routing_actions = (
        session.query(Action)
        .filter(Action.state == "in_progress", Action.kind == "rout")
        .all()
    )
    for action in routing_actions:
        _create_alert(
            session,
            recipient_commander_id=action.commander_id,
            alert_type="violence",
            signal_kind="state",
            category="battle",
            importance="high",
            message="Army out of control!",
            created_day=clock.day,
            created_watch=clock.watch,
        )


def _emit_stronghold_conquest_alerts(
    session: Session,
    *,
    stronghold: Stronghold,
    previous_faction: str,
    new_faction: str,
    clock: GameClock,
) -> None:
    event_date = _scenario_date_for_day(clock.day)
    watch_name = WATCH_LABELS.get(Watch(int(clock.watch)), "watch").capitalize()
    message = (
        f"{stronghold.stronghold_name} was conquered by {new_faction} forces "
        f"on {event_date.strftime('%B %d, %Y')}, {watch_name} Watch"
    )
    commanders = session.query(Commander).order_by(Commander.commander_id.asc()).all()
    for commander in commanders:
        commander_h3 = _commander_location_h3(session, commander.commander_id)
        commander_army = session.query(Army).filter(Army.commander_id == commander.commander_id).first()
        commander_faction = (commander_army.army_faction or "").strip() if commander_army else ""
        delay = 0
        if commander_h3:
            delay = max(0, _grid_distance(stronghold.location_id, commander_h3))
        delivered_day, delivered_watch = _advance_day_watch(clock.day, clock.watch, delay)
        importance = "high" if commander_faction and commander_faction == previous_faction else "normal"
        _create_alert(
            session,
            recipient_commander_id=commander.commander_id,
            alert_type="world event",
            signal_kind="event",
            category="territory",
            importance=importance,
            message=message,
            created_day=clock.day,
            created_watch=clock.watch,
            delivered_day=delivered_day,
            delivered_watch=delivered_watch,
            payload={
                "stronghold_id": _stronghold_ref(stronghold.stronghold_id),
                "stronghold_name": stronghold.stronghold_name,
                "previous_faction": previous_faction,
                "new_faction": new_faction,
                "location_h3": stronghold.location_id,
            },
        )


def _stronghold_at_h3(session: Session, location_h3: str) -> Stronghold | None:
    return (
        session.query(Stronghold)
        .filter(Stronghold.location_id == location_h3)
        .first()
    )


def _garrison_for_stronghold(session: Session, stronghold: Stronghold | None) -> Army | None:
    if stronghold is None:
        return None
    return (
        session.query(Army)
        .options(joinedload(Army.detachments))
        .filter(Army.garrison_stronghold_id == stronghold.stronghold_id, Army.is_garrison.is_(True))
        .first()
    )


def _set_stronghold_control(
    session: Session,
    *,
    stronghold: Stronghold,
    new_faction: str,
    clock: GameClock | None = None,
) -> None:
    previous_faction = str(stronghold.control or "").strip()
    new_faction = str(new_faction or "").strip()
    if previous_faction == new_faction:
        garrison = _garrison_for_stronghold(session, stronghold)
        if garrison is not None:
            garrison.army_faction = new_faction
            garrison.location_id = stronghold.location_id
            garrison.location = stronghold.location
        return
    stronghold.control = new_faction
    garrison = _garrison_for_stronghold(session, stronghold)
    if garrison is not None:
        garrison.army_faction = new_faction
        garrison.location_id = stronghold.location_id
        garrison.location = stronghold.location
    if clock is not None:
        record_history_event(
            session,
            event_key=f"conquest:{clock.world_tick}:{stronghold.stronghold_id}:{new_faction.casefold()}",
            world_tick=clock.world_tick,
            event_kind="stronghold_conquest",
            location_id=stronghold.location_id,
            payload={
                "stronghold_id": stronghold.stronghold_id,
                "stronghold_name": stronghold.stronghold_name,
                "stronghold_type": stronghold.stronghold_type,
                "location_h3": stronghold.location_id,
                "previous_controller": previous_faction,
                "new_controller": new_faction,
            },
        )
        _emit_stronghold_conquest_alerts(
            session,
            stronghold=stronghold,
            previous_faction=previous_faction,
            new_faction=new_faction,
            clock=clock,
        )


def _army_is_in_stronghold(session: Session, army: Army | None) -> bool:
    if army is None:
        return False
    return _stronghold_at_h3(session, army.location_id) is not None


def _army_is_under_siege(session: Session, army: Army | None) -> bool:
    if army is None:
        return False
    stronghold = _stronghold_at_h3(session, army.location_id)
    if stronghold is None:
        return False
    siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
    if siege is None:
        return False
    besieger_faction = _active_siege_faction(session, siege)
    if not besieger_faction:
        return False
    defenders = _defender_armies_in_stronghold(session, stronghold, besieger_faction)
    return any(defender.army_id == army.army_id for defender in defenders)


def _sortie_context_for_army(session: Session, army: Army | None) -> tuple[Stronghold | None, Siege | None]:
    if army is None:
        return None, None
    stronghold = _stronghold_at_h3(session, army.location_id)
    if stronghold is None:
        return None, None
    siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
    if siege is None:
        return None, None
    besieger_faction = _active_siege_faction(session, siege)
    if not besieger_faction:
        return None, None
    defenders = _defender_armies_in_stronghold(session, stronghold, besieger_faction)
    if any(defender.army_id == army.army_id for defender in defenders):
        return stronghold, siege
    return None, None


def _valid_stronghold_attack_targets(
    session: Session,
    army: Army,
) -> tuple[Stronghold | None, Siege | None, list[Army]]:
    """Return field armies an occupant may attack without leaving its stronghold."""
    stronghold = _stronghold_at_h3(session, army.location_id)
    if stronghold is None:
        return None, None, []
    try:
        adjacent_h3 = set(h3.grid_ring(army.location_id, 1))
    except Exception:
        return stronghold, None, []

    active_siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
    candidates: list[Army] = []
    if active_siege is not None:
        sortie_stronghold, sortie_siege = _sortie_context_for_army(session, army)
        if sortie_stronghold is None or sortie_siege is None:
            return stronghold, active_siege, []
        for participant in _active_siege_participants_for_siege(session, sortie_siege):
            candidate = session.get(Army, participant.besieger_army_id)
            if candidate is not None:
                candidates.append(candidate)
    else:
        candidates = (
            session.query(Army)
            .filter(
                Army.location_id.in_(list(adjacent_h3)),
                Army.army_id != army.army_id,
                Army.army_faction != army.army_faction,
                Army.is_garrison.is_(False),
            )
            .order_by(Army.army_id.asc())
            .all()
        )

    valid: list[Army] = []
    for candidate in candidates:
        if candidate.army_faction == army.army_faction or candidate.location_id not in adjacent_h3:
            continue
        if candidate.is_garrison or not _army_has_live_detachments(candidate):
            continue
        # Sallying into another occupied stronghold would bypass siege rules.
        if active_siege is None and _stronghold_at_h3(session, candidate.location_id) is not None:
            continue
        valid.append(candidate)
    valid.sort(key=lambda row: row.army_id)
    return stronghold, active_siege, valid


def _max_resistance_for_stronghold(stronghold: Stronghold) -> float:
    return float(SIEGE_RESISTANCE_BY_TYPE.get(str(stronghold.stronghold_type or "").strip().lower(), 10.0))


def _active_siege_for_stronghold(session: Session, stronghold_id: int) -> Siege | None:
    return (
        session.query(Siege)
        .filter(Siege.stronghold_id == stronghold_id, Siege.state == "active")
        .first()
    )


def _active_siege_participants_for_siege(session: Session, siege: Siege | None) -> list[SiegeParticipant]:
    if siege is None:
        return []
    return (
        session.query(SiegeParticipant)
        .filter(SiegeParticipant.siege_id == siege.siege_id, SiegeParticipant.state == "active")
        .order_by(SiegeParticipant.besieger_army_id.asc())
        .all()
    )


def _active_siege_participants_for_stronghold(session: Session, stronghold_id: int) -> list[SiegeParticipant]:
    siege = _active_siege_for_stronghold(session, stronghold_id)
    return _active_siege_participants_for_siege(session, siege)


def _active_siege_participant_for_army(session: Session, army_id: int) -> SiegeParticipant | None:
    return (
        session.query(SiegeParticipant)
        .filter(SiegeParticipant.besieger_army_id == army_id, SiegeParticipant.state == "active")
        .first()
    )


def _siege_participant_for_army(session: Session, siege_id: int, army_id: int) -> SiegeParticipant | None:
    return (
        session.query(SiegeParticipant)
        .filter(
            SiegeParticipant.siege_id == siege_id,
            SiegeParticipant.besieger_army_id == army_id,
        )
        .first()
    )


def _active_siege_for_besieger(session: Session, army_id: int) -> Siege | None:
    participant = _active_siege_participant_for_army(session, army_id)
    if participant is None:
        return None
    return session.get(Siege, participant.siege_id)


def _active_siege_faction(session: Session, siege: Siege | None) -> str | None:
    for participant in _active_siege_participants_for_siege(session, siege):
        besieger_army = session.get(Army, participant.besieger_army_id)
        if besieger_army is not None:
            faction = str(besieger_army.army_faction or "").strip()
            if faction:
                return faction
    return None


def _active_siege_commander_ids(session: Session, siege: Siege | None) -> set[int]:
    commander_ids: set[int] = set()
    for participant in _active_siege_participants_for_siege(session, siege):
        if participant.besieger_commander_id is not None:
            commander_ids.add(int(participant.besieger_commander_id))
    return commander_ids


def _sync_siege_lead_participant(session: Session, siege: Siege | None) -> None:
    if siege is None:
        return
    participants = _active_siege_participants_for_siege(session, siege)
    if not participants:
        return
    lead = participants[0]
    siege.besieger_army_id = lead.besieger_army_id
    siege.besieger_commander_id = lead.besieger_commander_id


def _emit_siege_world_event(
    session: Session,
    *,
    stronghold: Stronghold,
    message: str,
    clock: GameClock,
) -> None:
    commanders = session.query(Commander).order_by(Commander.commander_id.asc()).all()
    for commander in commanders:
        commander_h3 = _commander_location_h3(session, commander.commander_id)
        delay = max(0, _grid_distance(stronghold.location_id, commander_h3)) if commander_h3 else 0
        delivered_day, delivered_watch = _advance_day_watch(clock.day, clock.watch, delay)
        _create_alert(
            session,
            recipient_commander_id=commander.commander_id,
            alert_type="world event",
            signal_kind="event",
            category="territory",
            importance="normal",
            message=message,
            created_day=clock.day,
            created_watch=clock.watch,
            delivered_day=delivered_day,
            delivered_watch=delivered_watch,
            payload={
                "stronghold_id": _stronghold_ref(stronghold.stronghold_id),
                "stronghold_name": stronghold.stronghold_name,
                "location_h3": stronghold.location_id,
            },
        )


def _emit_siege_start_alerts(session: Session, *, stronghold: Stronghold, faction: str, clock: GameClock) -> None:
    event_date = _scenario_date_for_day(clock.day)
    watch_name = WATCH_LABELS.get(Watch(int(clock.watch)), "watch").capitalize()
    _emit_siege_world_event(
        session,
        stronghold=stronghold,
        message=(
            f"{stronghold.stronghold_name} was besieged by {faction} forces "
            f"on {event_date.strftime('%B %d, %Y')}, {watch_name} Watch"
        ),
        clock=clock,
    )


def _emit_siege_lifted_alerts(session: Session, *, stronghold: Stronghold, clock: GameClock) -> None:
    event_date = _scenario_date_for_day(clock.day)
    watch_name = WATCH_LABELS.get(Watch(int(clock.watch)), "watch").capitalize()
    _emit_siege_world_event(
        session,
        stronghold=stronghold,
        message=(
            f"The siege on {stronghold.stronghold_name} was lifted "
            f"on {event_date.strftime('%B %d, %Y')}, {watch_name} Watch"
        ),
        clock=clock,
    )


def _active_sieged_stronghold_ids(session: Session) -> set[int]:
    return {
        int(siege.stronghold_id)
        for siege in session.query(Siege).filter(Siege.state == "active").all()
    }


def _captured_sieged_stronghold_ids_for_watch(session: Session, clock: GameClock) -> set[int]:
    return {
        int(siege.stronghold_id)
        for siege in session.query(Siege)
        .filter(
            Siege.ended_day == clock.day,
            Siege.ended_watch == clock.watch,
            Siege.ended_reason == "captured",
        )
        .all()
    }


def _emit_siege_transition_alerts(
    session: Session,
    *,
    start_stronghold_ids: set[int],
    clock: GameClock,
) -> None:
    end_stronghold_ids = _active_sieged_stronghold_ids(session)
    captured_stronghold_ids = _captured_sieged_stronghold_ids_for_watch(session, clock)

    started_ids = sorted(end_stronghold_ids - start_stronghold_ids)
    lifted_ids = sorted((start_stronghold_ids - end_stronghold_ids) - captured_stronghold_ids)

    for stronghold_id in started_ids:
        stronghold = session.get(Stronghold, stronghold_id)
        siege = _active_siege_for_stronghold(session, stronghold_id)
        faction = _active_siege_faction(session, siege)
        if stronghold is None or not faction:
            continue
        _emit_siege_start_alerts(session, stronghold=stronghold, faction=faction, clock=clock)

    for stronghold_id in lifted_ids:
        stronghold = session.get(Stronghold, stronghold_id)
        if stronghold is None:
            continue
        _emit_siege_lifted_alerts(session, stronghold=stronghold, clock=clock)


def _emit_gates_open_alerts(
    session: Session,
    *,
    siege: Siege,
    stronghold: Stronghold,
    defender_commanders: list[int],
    clock: GameClock,
) -> None:
    recipient_ids = set(defender_commanders)
    recipient_ids.update(_active_siege_commander_ids(session, siege))
    for commander_id in sorted(recipient_ids):
        _create_alert(
            session,
            recipient_commander_id=commander_id,
            alert_type="world event",
            signal_kind="event",
            category="siege",
            importance="high",
            message=f"Gates of {stronghold.stronghold_name} opened from the inside!",
            created_day=clock.day,
            created_watch=clock.watch,
            payload={
                "stronghold_id": _stronghold_ref(stronghold.stronghold_id),
                "stronghold_name": stronghold.stronghold_name,
                "siege_id": siege.siege_id,
            },
        )


def _defender_armies_in_stronghold(
    session: Session,
    stronghold: Stronghold,
    enemy_faction: str,
    *,
    include_empty: bool = False,
) -> list[Army]:
    armies = (
        session.query(Army)
        .options(joinedload(Army.detachments), joinedload(Army.commander))
        .filter(
            Army.location_id == stronghold.location_id,
            Army.army_faction != enemy_faction,
        )
        .order_by(Army.army_id.asc())
        .all()
    )
    if include_empty:
        return armies
    return [army for army in armies if _army_has_live_detachments(army)]


def _capture_blockers_in_stronghold(
    session: Session,
    stronghold: Stronghold,
    attacker_faction: str,
) -> list[Army]:
    """Return hostile armies that must be cleared before capture.

    Empty field armies still represent commanders in the current game model, so
    they must be displaced rather than silently sharing the captured cell with
    the attacker. Empty garrison rows are retained as stronghold placeholders
    and change faction when control changes, so they are not capture blockers.
    """
    return [
        army
        for army in _defender_armies_in_stronghold(
            session,
            stronghold,
            attacker_faction,
            include_empty=True,
        )
        if not army.is_garrison or _army_has_live_detachments(army)
    ]


def _end_siege(
    session: Session,
    *,
    siege: Siege,
    clock: GameClock,
    reason: str,
    emit_lift_alert: bool = True,
) -> None:
    if siege.state != "active":
        return
    siege.state = "captured" if reason == "captured" else "lifted"
    siege.ended_day = clock.day
    siege.ended_watch = clock.watch
    siege.ended_reason = reason
    participants = _active_siege_participants_for_siege(session, siege)
    participant_commander_ids = {participant.besieger_commander_id for participant in participants if participant.besieger_commander_id is not None}
    for participant in participants:
        participant.state = "captured" if reason == "captured" else "lifted"
        participant.ended_day = clock.day
        participant.ended_watch = clock.watch
        participant.ended_reason = reason
    stronghold = session.get(Stronghold, siege.stronghold_id)
    record_history_event(
        session,
        event_key=f"siege:{siege.siege_id}:ended",
        world_tick=clock.world_tick,
        event_kind="siege_ended",
        location_id=stronghold.location_id if stronghold is not None else None,
        payload={
            "siege_id": siege.siege_id,
            "stronghold_id": siege.stronghold_id,
            "stronghold_name": stronghold.stronghold_name if stronghold is not None else None,
            "location_h3": stronghold.location_id if stronghold is not None else None,
            "reason": reason,
            "participant_army_ids": sorted(participant.besieger_army_id for participant in participants),
        },
    )
    if participant_commander_ids:
        besiege_actions = (
            session.query(Action)
            .filter(
                Action.commander_id.in_(list(participant_commander_ids)),
                Action.kind == "besiege",
                Action.state.in_(ACTIVE_ACTION_STATES),
            )
            .all()
        )
        for action in besiege_actions:
            action.state = "completed" if reason == "captured" else "cancelled"
    _ = emit_lift_alert


def _start_siege(
    session: Session,
    *,
    army: Army,
    commander_id: int,
    stronghold: Stronghold,
    clock: GameClock,
    action: Action,
) -> Siege:
    siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
    if siege is None:
        max_resistance = _max_resistance_for_stronghold(stronghold)
        siege = Siege(
            stronghold_id=stronghold.stronghold_id,
            besieger_army_id=army.army_id,
            besieger_commander_id=commander_id,
            started_day=clock.day,
            started_watch=clock.watch,
            matin_ticks_elapsed=0,
            current_resistance=max_resistance,
            max_resistance=max_resistance,
            gates_open=False,
            state="active",
        )
        session.add(siege)
        session.flush()
        record_history_event(
            session,
            event_key=f"siege:{siege.siege_id}:started",
            world_tick=clock.world_tick,
            event_kind="siege_started",
            location_id=stronghold.location_id,
            payload={
                "siege_id": siege.siege_id,
                "stronghold_id": stronghold.stronghold_id,
                "stronghold_name": stronghold.stronghold_name,
                "location_h3": stronghold.location_id,
                "besieger_faction": army.army_faction,
                "initial_army_id": army.army_id,
            },
        )
    existing_participant = _siege_participant_for_army(session, siege.siege_id, army.army_id)
    if existing_participant is None:
        session.add(
            SiegeParticipant(
                siege_id=siege.siege_id,
                besieger_army_id=army.army_id,
                besieger_commander_id=commander_id,
                started_day=clock.day,
                started_watch=clock.watch,
                state="active",
            )
        )
    else:
        existing_participant.besieger_commander_id = commander_id
        existing_participant.started_day = clock.day
        existing_participant.started_watch = clock.watch
        existing_participant.state = "active"
        existing_participant.ended_day = None
        existing_participant.ended_watch = None
        existing_participant.ended_reason = None
    session.flush()
    _sync_siege_lead_participant(session, siege)
    action.state = "in_progress"
    action.started_day = clock.day
    action.started_watch = clock.watch
    action.eta_day = None
    action.eta_watch = None
    return siege


def _restore_besiege_action_for_active_participant(
    session: Session,
    *,
    army: Army,
    commander_id: int,
    clock: GameClock,
) -> Action | None:
    """Restore the action-row view of siege duty after a temporary action."""
    siege = _active_siege_for_besieger(session, army.army_id)
    if siege is None or siege.state != "active":
        return None
    stronghold = session.get(Stronghold, siege.stronghold_id)
    if stronghold is None:
        return None
    action = Action(
        commander_id=commander_id,
        kind="besiege",
        state="in_progress",
        parameters_json=json.dumps(
            {
                "target_stronghold_id": stronghold.stronghold_id,
                "target_h3": stronghold.location_id,
                "target_stronghold_name": stronghold.stronghold_name,
            }
        ),
        accepted_at=datetime.now(timezone.utc),
        started_day=clock.day,
        started_watch=clock.watch,
        eta_day=None,
        eta_watch=None,
    )
    session.add(action)
    return action


def _abandon_active_siege_for_army(
    session: Session,
    *,
    army: Army,
    clock: GameClock,
    reason: str = "orders_changed",
) -> None:
    active_siege = _active_siege_for_besieger(session, army.army_id)
    if active_siege is not None:
        _remove_siege_participant(
            session,
            siege=active_siege,
            army_id=army.army_id,
            clock=clock,
            reason=reason,
        )


def _activate_besiege_action_at_transition(
    session: Session,
    *,
    action: Action,
    army: Army,
    clock: GameClock,
) -> bool:
    try:
        params = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        params = {}
    try:
        stronghold_id = int(params.get("target_stronghold_id"))
    except (TypeError, ValueError):
        action.state = "failed"
        return False
    stronghold = session.get(Stronghold, stronghold_id)
    if stronghold is None or stronghold.control == army.army_faction or _army_is_in_stronghold(session, army):
        action.state = "failed"
        return False
    try:
        adjacent = set(h3.grid_ring(army.location_id, 1))
    except Exception:
        adjacent = set()
    if stronghold.location_id not in adjacent:
        action.state = "failed"
        return False
    if not _defender_armies_in_stronghold(session, stronghold, army.army_faction):
        action.state = "failed"
        return False

    current_siege = _active_siege_for_besieger(session, army.army_id)
    if current_siege is not None:
        if int(current_siege.stronghold_id) != int(stronghold.stronghold_id):
            action.state = "failed"
            return False
        # Reasserting the same order is continuity, not a new siege.
        action.state = "in_progress"
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.eta_day = None
        action.eta_watch = None
        return True

    existing_siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
    if existing_siege is not None:
        besieger_faction = _active_siege_faction(session, existing_siege)
        if besieger_faction and besieger_faction != army.army_faction:
            action.state = "failed"
            return False
    _start_siege(
        session,
        army=army,
        commander_id=action.commander_id,
        stronghold=stronghold,
        clock=clock,
        action=action,
    )
    return True


def _find_active_siege_for_commander(session: Session, commander_id: int) -> Siege | None:
    participant = (
        session.query(SiegeParticipant)
        .filter(SiegeParticipant.besieger_commander_id == commander_id, SiegeParticipant.state == "active")
        .first()
    )
    if participant is None:
        return None
    return session.get(Siege, participant.siege_id)


def _remove_siege_participant(
    session: Session,
    *,
    siege: Siege,
    army_id: int,
    clock: GameClock,
    reason: str,
) -> None:
    participant = _active_siege_participant_for_army(session, army_id)
    if participant is None or participant.siege_id != siege.siege_id:
        return
    participant.state = "captured" if reason == "captured" else "lifted"
    participant.ended_day = clock.day
    participant.ended_watch = clock.watch
    participant.ended_reason = reason
    if participant.besieger_commander_id is not None:
        actions = (
            session.query(Action)
            .filter(
                Action.commander_id == participant.besieger_commander_id,
                Action.kind == "besiege",
                Action.state.in_(ACTIVE_ACTION_STATES),
            )
            .all()
        )
        for action in actions:
            action.state = "completed" if reason == "captured" else "cancelled"
    session.flush()
    remaining = _active_siege_participants_for_siege(session, siege)
    if not remaining:
        _end_siege(session, siege=siege, clock=clock, reason=reason)
        return
    _sync_siege_lead_participant(session, siege)


def _siege_assault_probability_open(resistance: float) -> float:
    return 1.0 - (1.0 - (1.0 / (1.0 + (math.e ** (1.25 * (float(resistance) - 7.0)))))) ** (1.0 / 7.0)


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
            _create_alert(
                session,
                recipient_commander_id=message.recipient_id,
                alert_type="report",
                signal_kind="event",
                category="messages",
                importance="normal",
                message=f"Letter received from {_message_sender_display_name(message)}.",
                created_day=clock.day,
                created_watch=clock.watch,
            )
            received += 1
    return {"received": received, "lost": lost}


def _advance_day_watch(day: int, watch: int, steps: int = 1) -> tuple[int, int]:
    next_day, next_watch = from_world_tick(to_world_tick(day, watch) + int(steps))
    return next_day, int(next_watch)


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


def _army_column_length(army: Army) -> float:
    infantry = _infantry_count(army)
    cavalry = _cavalry_count(army)
    wagons = sum(int(det.wagon_count or 0) for det in army.detachments)
    noncombatants = noncombatant_count(army)
    return 0.5 * (
        ((infantry + noncombatants) / 7500.0)
        + (cavalry / 3000.0)
        + (wagons / 75.0)
    )


def _army_has_long_column(army: Army) -> bool:
    return _army_column_length(army) > 2.0


def _army_is_cavalry_only(army: Army) -> bool:
    detachments = list(getattr(army, "detachments", []) or [])
    return bool(detachments) and all(bool(det.is_cavalry) for det in detachments)


def _forced_march_enabled_for_army(session: Session, army: Army | None) -> bool:
    if army is None or army.commander_id is None:
        return False
    standing = session.get(StandingOrder, army.commander_id)
    return bool(standing and standing.forced_march_enabled)


def _watch_is_at_or_after(day: int, watch: int, other_day: int, other_watch: int) -> bool:
    # Timeline ordering within a day is 1,2,3,4,0 (night comes last).
    watch_rank = {
        int(Watch.MATIN): 0,
        int(Watch.PRIME): 1,
        int(Watch.NOON): 2,
        int(Watch.VESPER): 3,
        int(Watch.NIGHT): 4,
    }
    return (day, watch_rank.get(int(watch), int(watch))) >= (
        other_day,
        watch_rank.get(int(other_watch), int(other_watch)),
    )


def _movement_slot_template(army: Army, forced_march: bool) -> list[int]:
    if _army_has_long_column(army):
        return [int(Watch.PRIME), int(Watch.PRIME), int(Watch.NOON), int(Watch.NOON)] if forced_march else [int(Watch.PRIME), int(Watch.NOON)]
    if forced_march and _army_is_cavalry_only(army):
        return [
            int(Watch.MATIN),
            int(Watch.MATIN),
            int(Watch.PRIME),
            int(Watch.PRIME),
            int(Watch.NOON),
            int(Watch.NOON),
            int(Watch.VESPER),
            int(Watch.VESPER),
        ]
    return (
        [int(Watch.MATIN), int(Watch.PRIME), int(Watch.PRIME), int(Watch.NOON), int(Watch.NOON), int(Watch.VESPER)]
        if forced_march
        else [int(Watch.MATIN), int(Watch.PRIME), int(Watch.NOON), int(Watch.VESPER)]
    )


def _movement_start_slot_stamps(day: int, watch: int, army: Army, forced_march: bool) -> list[tuple[int, int]]:
    template = _movement_slot_template(army, forced_march)
    if int(watch) == int(Watch.NIGHT):
        return [(int(day) + 1, slot) for slot in template]
    return [(int(day), slot) for slot in template if slot >= int(watch)]


def _movement_start_slots_for_watch(watch: int, army: Army, forced_march: bool) -> list[int]:
    return [slot_watch for _, slot_watch in _movement_start_slot_stamps(0, watch, army, forced_march)]


def _remaining_day_movement_budget_for_watch(watch: int, army: Army, forced_march: bool) -> int:
    return len(_movement_start_slot_stamps(0, watch, army, forced_march))


def _remaining_march_steps_for_watch(watch: int, army: Army | None = None, forced_march: bool = False) -> int:
    if army is None:
        if forced_march:
            template = [int(Watch.MATIN), int(Watch.PRIME), int(Watch.PRIME), int(Watch.NOON), int(Watch.NOON), int(Watch.VESPER)]
            return len(template) if int(watch) == int(Watch.NIGHT) else len([slot for slot in template if slot >= int(watch)])
        if watch <= int(Watch.MATIN):
            return 4
        return max(0, 5 - int(watch))
    return _remaining_day_movement_budget_for_watch(watch, army, forced_march)


def _remaining_march_watch_budget_for_watch(watch: int, army: Army | None = None, forced_march: bool = False) -> int:
    return _remaining_march_steps_for_watch(watch, army, forced_march)


def _movement_capacity_for_interval_start(start_watch: int, army: Army, forced_march: bool) -> int:
    return sum(1 for slot in _movement_slot_template(army, forced_march) if slot == int(start_watch))


def _watch_interval_start_for_current_watch(current_watch: int) -> int:
    return {
        int(Watch.MATIN): int(Watch.NIGHT),
        int(Watch.PRIME): int(Watch.MATIN),
        int(Watch.NOON): int(Watch.PRIME),
        int(Watch.VESPER): int(Watch.NOON),
        int(Watch.NIGHT): int(Watch.VESPER),
    }.get(int(current_watch), int(Watch.NIGHT))


def _watch_interval_start_stamp_for_current_watch(day: int, current_watch: int) -> tuple[int, int]:
    start_watch = _watch_interval_start_for_current_watch(current_watch)
    start_day = int(day)
    if int(current_watch) == int(Watch.MATIN):
        start_day -= 1
    return start_day, start_watch


def _move_remaining_cost(action: Action) -> int | None:
    try:
        params = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        return None
    remaining_cost = params.get("remaining_cost")
    if remaining_cost is None:
        return None
    try:
        return max(0, int(remaining_cost))
    except (TypeError, ValueError):
        return None


def _set_move_remaining_cost(action: Action, remaining_cost: int) -> None:
    try:
        params = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        params = {}
    params["remaining_cost"] = max(0, int(remaining_cost))
    action.parameters_json = json.dumps(params)


def _predicted_move_eta(
    *,
    day: int,
    watch: int,
    army: Army,
    remaining_cost: int,
    forced_march: bool,
) -> tuple[int, int] | None:
    if remaining_cost <= 0:
        return (int(day), int(watch))
    stamps = _movement_start_slot_stamps(day, watch, army, forced_march)
    if remaining_cost > len(stamps):
        return None
    last_day, last_start_watch = stamps[remaining_cost - 1]
    return _advance_day_watch(last_day, last_start_watch, 1)


def _refresh_move_eta(session: Session, action: Action, army: Army, *, day: int, watch: int) -> None:
    remaining_cost = _move_remaining_cost(action)
    if remaining_cost is None:
        action.eta_day = None
        action.eta_watch = None
        return
    eta = _predicted_move_eta(
        day=day,
        watch=watch,
        army=army,
        remaining_cost=remaining_cost,
        forced_march=_forced_march_enabled_for_army(session, army),
    )
    if eta is None:
        action.eta_day = None
        action.eta_watch = None
        return
    action.eta_day, action.eta_watch = eta


def _long_column_start_blocked(army: Army, watch: int) -> bool:
    return _army_has_long_column(army) and watch in {
        int(Watch.NIGHT),
        int(Watch.MATIN),
        int(Watch.VESPER),
    }


def _long_column_completion_blocked(army: Army, watch: int) -> bool:
    return _army_has_long_column(army) and watch in {
        int(Watch.NIGHT),
        int(Watch.MATIN),
    }


def _scenario_date_for_day(day: int) -> date:
    return SCENARIO_EPOCH + timedelta(days=max(day, 0))


def _get_destination_h3(action: Action) -> str | None:
    try:
        payload = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        return None
    destination_h3 = payload.get("destination_h3")
    if isinstance(destination_h3, str) and destination_h3:
        return destination_h3
    return None


def _forage_depletion_level(location: Location) -> int:
    return forage_depletion_level(location.foraged_this_season)


def _forage_yield_for_location(location: Location) -> Fraction:
    settlement_yield = max(int(location.settlement or 0), 0) * 2500
    depletion = _forage_depletion_level(location)
    return Fraction(settlement_yield * (3 - depletion), 3 * (depletion + 1))


def _forage_supply_gain_for_army(session: Session, army: Army) -> tuple[int, list[Location], float]:
    radius = _environs_radius_for_army(army)
    visible_h3 = list(h3.grid_disk(army.location_id, radius))
    locations = session.query(Location).filter(Location.location_id.in_(visible_h3)).all()
    enemy_occupied_h3 = {
        str(location_id)
        for (location_id,) in session.query(Army.location_id)
        .filter(
            Army.location_id.in_(visible_h3),
            Army.army_id != army.army_id,
            Army.army_faction != army.army_faction,
        )
        .distinct()
        .all()
    }
    # A besieged stronghold remains inaccessible even if its garrison happens to
    # have no surviving Army row. The siege target is never part of the besieger's
    # forage country.
    active_siege = _active_siege_for_besieger(session, army.army_id)
    if active_siege is not None:
        besieged_stronghold = session.get(Stronghold, active_siege.stronghold_id)
        if besieged_stronghold is not None:
            enemy_occupied_h3.add(str(besieged_stronghold.location_id))
    forageable_locations = [
        location
        for location in locations
        if int(location.settlement or 0) > 0 and location.location_id not in enemy_occupied_h3
    ]
    exact_gain = sum((_forage_yield_for_location(location) for location in forageable_locations), Fraction(0, 1))
    average_depletion = (
        sum(_forage_depletion_level(location) for location in forageable_locations) / len(forageable_locations)
        if forageable_locations
        else 3.0
    )
    return int(exact_gain), forageable_locations, average_depletion


def _initialize_move_action_progress(
    session: Session,
    action: Action,
    army: Army,
    *,
    start_day: int,
    start_watch: int,
) -> bool:
    destination_h3 = _get_destination_h3(action)
    if destination_h3 is None:
        action.state = "failed"
        return False
    if destination_h3 == army.location_id:
        action.state = "completed"
        action.started_day = start_day
        action.started_watch = start_watch
        action.eta_day = start_day
        action.eta_watch = start_watch
        _set_move_remaining_cost(action, 0)
        return True
    try:
        total_cost = calculate_move_watches(session, army.army_id, destination_h3)
    except ValueError:
        action.state = "failed"
        return False
    action.started_day = start_day
    action.started_watch = start_watch
    action.state = "in_progress"
    _set_move_remaining_cost(action, total_cost)
    _refresh_move_eta(session, action, army, day=start_day, watch=start_watch)
    return True


def _serialize_standing_orders(standing: StandingOrder | None) -> dict[str, Any]:
    if standing is None:
        return {
            "follow_road": {
                "enabled": False,
                "last_report": None,
                "last_report_day": None,
                "last_report_watch": None,
            },
            "forced_march": {
                "enabled": False,
            },
        }
    return {
        "follow_road": {
            "enabled": bool(standing.follow_road_enabled),
            "last_report": standing.last_report,
            "last_report_day": standing.last_report_day,
            "last_report_watch": standing.last_report_watch,
        },
        "forced_march": {
            "enabled": bool(standing.forced_march_enabled),
        },
    }


def _get_or_create_standing_order(session: Session, commander_id: int) -> StandingOrder:
    standing = session.get(StandingOrder, commander_id)
    if standing is not None:
        return standing
    standing = StandingOrder(
        commander_id=commander_id,
        follow_road_enabled=False,
        forced_march_enabled=False,
        last_report=None,
        last_report_day=None,
        last_report_watch=None,
        updated_at=datetime.now(timezone.utc),
    )
    session.add(standing)
    session.flush()
    return standing


def _set_standing_order_report(
    session: Session,
    standing: StandingOrder,
    *,
    clock: GameClock,
    message: str,
) -> None:
    importance = "moderate" if "new orders needed" in message.lower() else "normal"
    standing.last_report = message
    standing.last_report_day = clock.day
    standing.last_report_watch = clock.watch
    standing.updated_at = datetime.now(timezone.utc)
    _create_alert(
        session,
        recipient_commander_id=standing.commander_id,
        alert_type="report",
        signal_kind="event",
        category="standing-order",
        importance=importance,
        message=message,
        created_day=clock.day,
        created_watch=clock.watch,
    )


def _auto_disable_forced_march_at_night(session: Session, clock: GameClock) -> None:
    if int(clock.watch) != int(Watch.NIGHT):
        return
    standing_rows = (
        session.query(StandingOrder)
        .filter(StandingOrder.forced_march_enabled.is_(True))
        .all()
    )
    for standing in standing_rows:
        standing.forced_march_enabled = False
        standing.updated_at = datetime.now(timezone.utc)
        army = session.query(Army).filter(Army.commander_id == standing.commander_id).first()
        if army is None or army.is_garrison:
            continue
        _run_morale_test_for_army(
            session,
            army=army,
            clock=clock,
            category="standing-order",
        )
        if random.randint(1, 6) != 1:
            continue
        army.army_morale = _clamp_morale(int(army.army_morale or 0) - 1)
        _create_alert(
            session,
            recipient_commander_id=standing.commander_id,
            alert_type="action",
            signal_kind="event",
            category="standing-order",
            importance="moderate",
            message="Morale suffering from forced march",
            created_day=clock.day,
            created_watch=clock.watch,
        )


def _forced_march_is_locked_for_watch(watch: int) -> bool:
    return int(watch) in {int(Watch.PRIME), int(Watch.NOON), int(Watch.VESPER)}


def _latest_previous_location_for_army(session: Session, army: Army) -> str | None:
    rows = (
        session.query(Movement.location_id)
        .filter(Movement.army_id == army.army_id)
        .order_by(Movement.date.desc(), Movement.watch.desc())
        .limit(24)
        .all()
    )
    current_h3 = army.location_id
    for (location_id,) in rows:
        if location_id != current_h3:
            return str(location_id)
    return None


def _next_road_cell(
    session: Session,
    army: Army,
    *,
    current_h3: str,
    last_h3: str,
) -> tuple[str | None, str | None, str | None]:
    current_location = session.get(Location, current_h3)
    if current_location is None or not bool(current_location.is_road):
        return None, "off_road", "Road march halted: army is no longer on a road."

    try:
        adjacent = set(h3.grid_ring(current_h3, 1))
    except Exception:
        return None, "error", "Road march halted: unable to inspect adjacent cells."

    if not adjacent:
        return None, "dead_end", "Road march halted: no adjacent cells."

    road_adjacent = {
        row[0]
        for row in session.query(Location.location_id)
        .filter(Location.location_id.in_(adjacent), Location.is_road.is_(True))
        .all()
    }
    valid_from_current = set(list_valid_destinations_from_origin(session, army.army_id, current_h3))
    candidates = sorted(
        cell_h3
        for cell_h3 in road_adjacent
        if cell_h3 != last_h3 and cell_h3 in valid_from_current
    )
    if len(candidates) == 1:
        return candidates[0], None, None
    if len(candidates) == 0:
        return None, "dead_end", "Road march halted: no onward road; new orders needed."
    return None, "crossroads", "Crossroads reached, new orders needed."


def _apply_plan(
    session: Session,
    *,
    commander_id: int,
    army: Army,
    clock: GameClock,
    kind: str,
    path: list[str],
    now: datetime,
    allow_partial_night_march: bool = False,
    disable_follow_road: bool = False,
) -> tuple[list[Action], int, dict[str, int]]:
    _ = disable_follow_road
    active_actions = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )
    for existing in active_actions:
        existing.state = "cancelled"

    created_actions: list[Action] = []
    if kind == "forage":
        if clock.watch not in {int(Watch.NIGHT), int(Watch.MATIN)}:
            raise HTTPException(status_code=400, detail="Forage orders may only be submitted during Night or Matin watch")
        if _army_is_under_siege(session, army):
            raise HTTPException(status_code=400, detail="Armies under siege cannot forage")
        action = Action(
            commander_id=commander_id,
            kind="forage",
            state="queued",
            parameters_json=json.dumps({}),
            accepted_at=now,
        )
        session.add(action)
        created_actions.append(action)
    elif kind == "march":
        if path:
            forced_march = _forced_march_enabled_for_army(session, army)
            max_steps = _remaining_march_steps_for_watch(int(clock.watch), army, forced_march)
            if len(path) > max_steps:
                raise HTTPException(
                    status_code=400,
                    detail=f"March path too long for current watch: max {max_steps} cells, got {len(path)}",
                )
            total_watch_cost = _path_watches_for_army(session, army, army.location_id, path)
            max_budget = _remaining_march_watch_budget_for_watch(int(clock.watch), army, forced_march)
            if total_watch_cost > max_budget:
                raise HTTPException(
                    status_code=400,
                    detail="March path exceeds remaining watch budget for this day.",
                )
            for destination_h3 in path:
                if session.get(Location, destination_h3) is None:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": "Unknown move destination_h3",
                            "destination_h3": destination_h3,
                        },
                    )
                action = Action(
                    commander_id=commander_id,
                    kind="move",
                    state="queued",
                    parameters_json=json.dumps({"destination_h3": destination_h3}),
                    accepted_at=now,
                )
                session.add(action)
                created_actions.append(action)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported plan kind: {kind}")

    in_progress_exists = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state == "in_progress")
        .first()
        is not None
    )
    if created_actions and not in_progress_exists:
        _start_action_now_if_valid(session, created_actions[0], army, clock)

    preserves_siege_duty = kind == "forage" and _active_siege_for_besieger(session, army.army_id) is not None
    cancelled_by_kind: dict[str, int] = {}
    for existing in active_actions:
        existing_kind = (existing.kind or "unknown").strip().lower()
        # The besiege action row yields the single active-action slot to forage,
        # but SiegeParticipant remains authoritative and active. Do not report
        # that bookkeeping change to the player as a cancelled siege order.
        if preserves_siege_duty and existing_kind == "besiege":
            continue
        cancelled_by_kind[existing_kind] = cancelled_by_kind.get(existing_kind, 0) + 1

    return created_actions, sum(cancelled_by_kind.values()), cancelled_by_kind


def _ordered_active_actions_for_commander(session: Session, commander_id: int) -> list[Action]:
    return (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )


def _append_follow_road_moves(
    session: Session,
    *,
    commander_id: int,
    army: Army,
    clock: GameClock,
    path: list[str],
    now: datetime,
) -> None:
    if not path:
        return
    for destination_h3 in path:
        action = Action(
            commander_id=commander_id,
            kind="move",
            state="queued",
            parameters_json=json.dumps({"destination_h3": destination_h3}),
            accepted_at=now,
        )
        session.add(action)

    in_progress_exists = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state == "in_progress")
        .first()
        is not None
    )
    if not in_progress_exists:
        first_queued = (
            session.query(Action)
            .filter(
                Action.commander_id == commander_id,
                Action.state == "queued",
                Action.kind == "move",
            )
            .order_by(Action.accepted_at.asc(), Action.action_id.asc())
            .first()
        )
        if first_queued is not None:
            _start_action_now_if_valid(session, first_queued, army, clock)


def _auto_apply_follow_road_orders(session: Session, clock: GameClock) -> None:
    # Ensure movement rows created earlier in this same tick are visible to
    # previous-position lookup before building or extending standing-order plans.
    session.flush()
    standing_rows = (
        session.query(StandingOrder)
        .filter(StandingOrder.follow_road_enabled.is_(True))
        .all()
    )
    for standing in standing_rows:
        army = session.query(Army).filter(Army.commander_id == standing.commander_id).first()
        if army is None:
            continue
        if _active_siege_for_besieger(session, army.army_id) is not None:
            continue
        active_actions = _ordered_active_actions_for_commander(session, standing.commander_id)
        if any(action.kind not in {"move"} for action in active_actions):
            continue

        move_actions = [action for action in active_actions if action.kind == "move"]
        forced_march = bool(standing.forced_march_enabled)
        max_budget = _remaining_march_watch_budget_for_watch(int(clock.watch), army, forced_march)
        queued_path = [
            destination_h3
            for action in move_actions
            for destination_h3 in [_get_destination_h3(action)]
            if destination_h3
        ]
        try:
            current_watch_cost = _path_watches_for_army(session, army, army.location_id, queued_path)
        except ValueError:
            standing.follow_road_enabled = False
            standing.updated_at = datetime.now(timezone.utc)
            _set_standing_order_report(
                session,
                standing,
                clock=clock,
                message="Road march halted: queued route no longer contiguous; new orders needed.",
            )
            continue
        if current_watch_cost >= max_budget:
            continue

        previous_h3 = _latest_previous_location_for_army(session, army)
        if not previous_h3:
            continue

        current_h3 = army.location_id
        last_h3 = previous_h3
        if move_actions:
            itinerary_points: list[str] = [army.location_id]
            for action in move_actions:
                destination_h3 = _get_destination_h3(action)
                if destination_h3:
                    itinerary_points.append(destination_h3)
            if len(itinerary_points) >= 2:
                last_h3 = itinerary_points[-2]
                current_h3 = itinerary_points[-1]

        extension_path: list[str] = []
        stop_reason: str | None = None
        stop_reason_code: str | None = None
        extension_watch_cost = 0
        while current_watch_cost + extension_watch_cost < max_budget:
            next_h3, reason_code, reason = _next_road_cell(
                session,
                army,
                current_h3=current_h3,
                last_h3=last_h3,
            )
            if next_h3 is None:
                stop_reason_code = reason_code
                stop_reason = reason or "Road march halted: unable to continue."
                break
            step_cost = calculate_move_watches_from_origin(session, army.army_id, current_h3, next_h3)
            if current_watch_cost + extension_watch_cost + step_cost > max_budget:
                break
            extension_path.append(next_h3)
            extension_watch_cost += step_cost
            last_h3, current_h3 = current_h3, next_h3

        if not extension_path:
            continue

        if not move_actions and clock.watch == int(Watch.NIGHT):
            try:
                _apply_plan(
                    session,
                    commander_id=standing.commander_id,
                    army=army,
                    clock=clock,
                    kind="march",
                    path=extension_path,
                    now=datetime.now(timezone.utc),
                    allow_partial_night_march=True,
                )
            except HTTPException:
                continue
            standing.last_report = "Next day's march planned according to standing orders"
            standing.last_report_day = clock.day
            standing.last_report_watch = clock.watch
            standing.updated_at = datetime.now(timezone.utc)
            _create_alert(
                session,
                recipient_commander_id=standing.commander_id,
                alert_type="action",
                signal_kind="event",
                category="standing-order",
                importance="normal",
                message="Next day's march planned according to standing orders",
                created_day=clock.day,
                created_watch=clock.watch,
            )
            if stop_reason and not (stop_reason_code in {"crossroads", "dead_end"} and extension_path):
                _set_standing_order_report(
                    session,
                    standing,
                    clock=clock,
                    message=stop_reason,
                )
            continue

        _append_follow_road_moves(
            session,
            commander_id=standing.commander_id,
            army=army,
            clock=clock,
            path=extension_path,
            now=datetime.now(timezone.utc),
        )


def _infantry_count(army: Army) -> int:
    return sum(int(det.warrior_count or 0) for det in army.detachments if not det.is_cavalry)


def _cavalry_count(army: Army) -> int:
    return sum(int(det.warrior_count or 0) for det in army.detachments if det.is_cavalry)


def _effective_strength(army: Army, *, engagement_type: str = "field") -> int:
    mode = str(engagement_type or "field").strip().lower()
    total = 0
    for det in army.detachments:
        warriors = int(det.warrior_count or 0)
        if warriors <= 0:
            continue
        if mode == "siege":
            if det.is_heavy and not det.is_cavalry:
                total += 2 * warriors
            else:
                total += warriors
            continue
        if det.is_cavalry and det.is_heavy:
            total += 4 * warriors
        elif det.is_cavalry or det.is_heavy:
            total += 2 * warriors
        else:
            total += warriors
    return total


def _ratio_label(numerator: int, denominator: int) -> str | None:
    if numerator <= 0 or denominator <= 0:
        return None
    ratio = numerator / denominator
    rounded = int(round(ratio))
    if rounded < 2:
        return None
    return f"{rounded}-to-1"


def _is_enemy_occupied(session: Session, *, destination_h3: str, moving_army: Army) -> bool:
    blocker = (
        session.query(Army.army_id)
        .join(Detachment, Detachment.army_id == Army.army_id)
        .filter(
            Army.location_id == destination_h3,
            Army.army_id != moving_army.army_id,
            Army.army_faction != moving_army.army_faction,
            Detachment.warrior_count > 0,
        )
        .first()
    )
    return blocker is not None


def _nearest_distance_to_armies(origin_h3: str, armies: list[Army]) -> int:
    if not armies:
        return 0
    return min(max(0, _grid_distance(origin_h3, army.location_id)) for army in armies)


def _active_non_attack_kind(session: Session, commander_id: int, exclude_action_ids: set[int] | None = None) -> str | None:
    exclude_action_ids = exclude_action_ids or set()
    row = (
        session.query(Action)
        .filter(
            Action.commander_id == commander_id,
            Action.state == "in_progress",
            Action.action_id.notin_(list(exclude_action_ids)) if exclude_action_ids else True,
        )
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .first()
    )
    if row is None:
        return None
    return str(row.kind or "").strip().lower() or None


def _apply_morale_delta(army: Army, delta: int) -> None:
    if army.is_garrison:
        return
    army.army_morale = _clamp_morale(int(army.army_morale or 0) + int(delta))


def _halve_army(session: Session, army: Army) -> tuple[int, int]:
    warriors_before = sum(int(det.warrior_count or 0) for det in army.detachments)
    for det in army.detachments:
        det.warrior_count = max(0, int(det.warrior_count or 0) // 2)
    supply_before = max(0, int(army.army_supply or 0))
    army.army_supply = supply_before // 2
    return warriors_before - sum(int(det.warrior_count or 0) for det in army.detachments), supply_before - int(army.army_supply or 0)


def _path_watches_for_army(session: Session, army: Army, origin_h3: str, path: list[str]) -> int:
    if not path:
        return 0
    total = 0
    current = origin_h3
    for destination in path:
        total += calculate_move_watches_from_origin(session, army.army_id, current, destination)
        current = destination
    return total


def _build_rout_path(
    session: Session,
    *,
    army: Army,
    winner_armies: list[Army],
    start_h3: str,
    steps: int,
) -> list[str]:
    if steps <= 0:
        return []
    path: list[str] = []
    current = start_h3
    for _ in range(steps):
        try:
            candidates = list_valid_destinations_from_origin(session, army.army_id, current)
        except ValueError:
            break
        if not candidates:
            break
        current_distance = _nearest_distance_to_armies(current, winner_armies)
        scored: list[tuple[int, str]] = []
        for candidate in candidates:
            if _is_enemy_occupied(session, destination_h3=candidate, moving_army=army):
                continue
            d = _nearest_distance_to_armies(candidate, winner_armies)
            if d > current_distance:
                scored.append((d, candidate))
        if not scored:
            break
        scored.sort(key=lambda item: item[0], reverse=True)
        max_d = scored[0][0]
        best = [candidate for d, candidate in scored if d == max_d]
        next_h3 = random.choice(best)
        path.append(next_h3)
        current = next_h3
    return path


def _retreat_one_cell(
    session: Session,
    *,
    army: Army,
    winner_armies: list[Army],
    clock: GameClock,
) -> bool:
    if army.is_garrison:
        _destroy_army(session, army, clock=clock)
        return False
    current_h3 = army.location_id
    current_distance = _nearest_distance_to_armies(current_h3, winner_armies)
    candidates = []
    for candidate in list_valid_destinations(session, army.army_id):
        if _is_enemy_occupied(session, destination_h3=candidate, moving_army=army):
            continue
        d = _nearest_distance_to_armies(candidate, winner_armies)
        if d > current_distance:
            candidates.append(candidate)
    if not candidates:
        return False
    destination_h3 = random.choice(candidates)
    return _execute_move_to_destination(session, clock, army, destination_h3)


def _destroy_army(session: Session, army: Army, *, clock: GameClock | None = None) -> None:
    archived_army = army_history_payload(army) if not army.is_garrison and clock is not None else None
    if army.is_garrison:
        for det in list(army.detachments):
            det.warrior_count = 0
            session.delete(det)
        return
    if archived_army is not None and clock is not None:
        record_history_event(
            session,
            event_key=f"army:{army.army_id}:destroyed:{clock.world_tick}",
            world_tick=clock.world_tick,
            event_kind="army_destroyed",
            location_id=army.location_id,
            payload={"army": archived_army, "location_h3": army.location_id},
        )
    for det in list(army.detachments):
        det.warrior_count = 0
        session.delete(det)
    session.delete(army)


def _drop_wagons_for_army(army: Army) -> None:
    for det in army.detachments:
        det.wagon_count = 0


def _retreat_one_cell_with_wagon_drop(
    session: Session,
    *,
    army: Army,
    winner_armies: list[Army],
    clock: GameClock,
) -> dict[str, Any]:
    if army.is_garrison:
        _destroy_army(session, army, clock=clock)
        return {"retreated": False, "dropped_wagons": False, "destroyed": True, "garrison_destroyed": True}
    retreat_ok = _retreat_one_cell(session, army=army, winner_armies=winner_armies, clock=clock)
    if retreat_ok:
        return {"retreated": True, "dropped_wagons": False, "destroyed": False}
    has_wagons = any(int(det.wagon_count or 0) > 0 for det in army.detachments)
    if has_wagons:
        _drop_wagons_for_army(army)
        retreat_ok = _retreat_one_cell(session, army=army, winner_armies=winner_armies, clock=clock)
        if retreat_ok:
            return {"retreated": True, "dropped_wagons": True, "destroyed": False}
    _destroy_army(session, army, clock=clock)
    return {"retreated": False, "dropped_wagons": has_wagons, "destroyed": True}


def _create_rout_action(
    session: Session,
    *,
    army: Army,
    commander_id: int,
    clock: GameClock,
    path: list[str],
    source_battle: dict[str, Any],
) -> Action | None:
    if not path:
        return None
    active_siege = _active_siege_for_besieger(session, army.army_id)
    if active_siege is not None:
        _remove_siege_participant(session, siege=active_siege, army_id=army.army_id, clock=clock, reason="besieger_routed")
    active = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
        .all()
    )
    for row in active:
        row.state = "cancelled"
    first_destination = str(path[0]).strip() if path else ""
    if not first_destination:
        return None
    watches_needed = calculate_move_watches_from_origin(session, army.army_id, army.location_id, first_destination)
    eta_day, eta_watch = _advance_day_watch(clock.day, clock.watch, watches_needed)
    action = Action(
        commander_id=commander_id,
        kind="rout",
        state="in_progress",
        parameters_json=json.dumps({"path": path, "source_battle": source_battle}),
        accepted_at=datetime.now(timezone.utc),
        started_day=clock.day,
        started_watch=clock.watch,
        eta_day=eta_day,
        eta_watch=eta_watch,
    )
    session.add(action)
    return action


def _schedule_next_rout_leg(
    session: Session,
    *,
    action: Action,
    army: Army,
    clock: GameClock,
    remaining_path: list[str],
    source_battle: dict[str, Any],
) -> bool:
    if not remaining_path:
        return False
    next_destination = str(remaining_path[0]).strip()
    if not next_destination:
        return False
    watches_needed = calculate_move_watches_from_origin(session, army.army_id, army.location_id, next_destination)
    action.parameters_json = json.dumps({"path": remaining_path, "source_battle": source_battle})
    action.started_day = clock.day
    action.started_watch = clock.watch
    action.eta_day, action.eta_watch = _advance_day_watch(clock.day, clock.watch, watches_needed)
    action.state = "in_progress"
    return True


def _displace_empty_hostile_field_armies_for_entry(
    session: Session,
    *,
    clock: GameClock,
    moving_army: Army,
    destination_h3: str,
) -> bool:
    if not _army_has_live_detachments(moving_army):
        return True

    session.flush()
    occupants = (
        session.query(Army)
        .options(joinedload(Army.detachments))
        .filter(
            Army.location_id == destination_h3,
            Army.army_id != moving_army.army_id,
            Army.army_faction != moving_army.army_faction,
            Army.is_garrison.is_(False),
        )
        .order_by(Army.army_id.asc())
        .all()
    )
    empty_occupants = [army for army in occupants if not _army_has_live_detachments(army)]
    if not empty_occupants:
        return True

    planned_retreats: list[tuple[Army, str]] = []
    for occupant in empty_occupants:
        candidates = [
            candidate
            for candidate in list_valid_destinations(session, occupant.army_id)
            if not _is_enemy_occupied(session, destination_h3=candidate, moving_army=occupant)
        ]
        if not candidates:
            return False
        planned_retreats.append((occupant, random.choice(candidates)))

    for occupant, retreat_h3 in planned_retreats:
        if not _execute_move_to_destination(session, clock, occupant, retreat_h3):
            return False
    session.flush()
    return True


def _execute_move_to_destination(session: Session, clock: GameClock, army: Army, destination_h3: str) -> bool:
    destination_location = session.get(Location, destination_h3)
    if destination_location is None:
        return False
    stronghold = _stronghold_at_h3(session, destination_h3)
    if stronghold is not None:
        hostile_occupant = (
            session.query(Army.army_id)
            .join(Detachment, Detachment.army_id == Army.army_id)
            .filter(
                Army.location_id == destination_h3,
                Army.army_id != army.army_id,
                Army.army_faction != army.army_faction,
                Detachment.warrior_count > 0,
            )
            .first()
        )
        if hostile_occupant is not None:
            return False
    if not _displace_empty_hostile_field_armies_for_entry(
        session,
        clock=clock,
        moving_army=army,
        destination_h3=destination_h3,
    ):
        return False
    army.location = destination_location
    army.location_id = destination_h3
    session.add(
        Movement(
            army_id=army.army_id,
            location_id=destination_h3,
            date=_scenario_date_for_day(clock.day),
            watch=clock.watch,
        )
    )
    if stronghold is not None and stronghold.control != army.army_faction:
        _set_stronghold_control(session, stronghold=stronghold, new_faction=army.army_faction, clock=clock)
    if army.commander_id is not None:
        active_siege = _active_siege_for_besieger(session, army.army_id)
        if active_siege is not None:
            siege_stronghold = session.get(Stronghold, active_siege.stronghold_id)
            still_adjacent = False
            if siege_stronghold is not None:
                try:
                    still_adjacent = (
                        army.location_id == siege_stronghold.location_id
                        or siege_stronghold.location_id in set(h3.grid_ring(army.location_id, 1))
                    )
                except Exception:
                    still_adjacent = False
            if not still_adjacent:
                _remove_siege_participant(
                    session,
                    siege=active_siege,
                    army_id=army.army_id,
                    clock=clock,
                    reason="besieger_displaced",
                )
    return True


def _resolve_battles_from_edges(
    session: Session,
    clock: GameClock,
    *,
    action_by_id: dict[int, Action],
    edges: list[tuple[int, int, int]],
    target_h3_by_action_id: dict[int, str],
    target_army_id_by_action_id: dict[int, int],
    forced_out_of_formation_army_ids: set[int] | None = None,
    disable_surprise_army_ids: set[int] | None = None,
    allow_side_draw: bool = False,
    winner_destination_by_action_id: dict[int, str] | None = None,
    battle_copy_mode: str = "attack",
    engagement_type: str = "field",
    attacker_flat_modifier: int = 0,
    defender_flat_modifier_by_army_id: dict[int, int] | None = None,
    loser_extra_casualty_pct: float = 0.0,
    attacker_can_retreat_or_rout: bool = True,
    sortie_attacker_army_ids: set[int] | None = None,
) -> dict[str, Any]:
    if not edges:
        return {"completed": 0, "failed": 0, "winner_faction_by_action_id": {}}

    forced_out_of_formation_army_ids = forced_out_of_formation_army_ids or set()
    disable_surprise_army_ids = disable_surprise_army_ids or set()
    winner_destination_by_action_id = winner_destination_by_action_id or {}
    defender_flat_modifier_by_army_id = defender_flat_modifier_by_army_id or {}
    sortie_attacker_army_ids = sortie_attacker_army_ids or set()

    adjacency: dict[int, set[int]] = defaultdict(set)
    edge_action_ids_by_node: dict[int, set[int]] = defaultdict(set)
    for action_id, attacker_id, target_id in edges:
        adjacency[attacker_id].add(target_id)
        adjacency[target_id].add(attacker_id)
        edge_action_ids_by_node[attacker_id].add(action_id)
        edge_action_ids_by_node[target_id].add(action_id)

    visited: set[int] = set()
    components: list[set[int]] = []
    for node in adjacency:
        if node in visited:
            continue
        stack = [node]
        comp = set()
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.add(cur)
            stack.extend(list(adjacency.get(cur, set()) - visited))
        components.append(comp)

    failed = 0
    completed = 0
    winner_faction_by_action_id: dict[int, str | None] = {}

    for comp_idx, participant_ids in enumerate(components, start=1):
        participant_armies = [session.get(Army, army_id) for army_id in participant_ids]
        participant_armies = [army for army in participant_armies if army is not None]
        if len(participant_armies) < 2:
            for army_id in participant_ids:
                for action_id in edge_action_ids_by_node.get(army_id, set()):
                    action = action_by_id.get(action_id)
                    if action is not None and action.state == "in_progress":
                        action.state = "failed"
                        failed += 1
            continue

        action_ids_in_component: set[int] = set()
        attacker_ids: set[int] = set()
        incoming_by_target: dict[int, list[int]] = defaultdict(list)
        outgoing_action_ids_by_attacker: dict[int, list[int]] = defaultdict(list)
        for action_id, attacker_id, target_id in edges:
            if attacker_id in participant_ids and target_id in participant_ids:
                action_ids_in_component.add(action_id)
                attacker_ids.add(attacker_id)
                incoming_by_target[target_id].append(action_id)
                outgoing_action_ids_by_attacker[attacker_id].append(action_id)

        sides: dict[str, list[Army]] = defaultdict(list)
        for army in participant_armies:
            sides[str(army.army_faction)].append(army)
        if len(sides) < 2:
            for action_id in action_ids_in_component:
                action = action_by_id.get(action_id)
                if action is not None and action.state == "in_progress":
                    action.state = "failed"
                    failed += 1
            continue

        participant_before = {
            army.army_id: army_history_payload(army)
            for army in sorted(participant_armies, key=lambda row: row.army_id)
        }

        side_strength: dict[str, int] = {
            faction: sum(_effective_strength(army, engagement_type=engagement_type) for army in armies)
            for faction, armies in sides.items()
        }
        side_top_roll: dict[str, int] = {}
        army_final_roll: dict[int, int] = {}
        army_modifiers: dict[int, dict[str, int]] = {}
        side_top_army_id: dict[str, int] = {}
        all_attack_action_ids = set(action_ids_in_component)

        for army in participant_armies:
            faction = str(army.army_faction)
            enemy_factions = [f for f in sides.keys() if f != faction]
            enemy_side_strength = max((side_strength.get(f, 1) for f in enemy_factions), default=1)
            own_side_strength = max(1, side_strength.get(faction, 1))
            numerical_adv = max(0, int(math.floor((own_side_strength / max(enemy_side_strength, 1)) - 1.0)))
            highest_enemy_morale = max(
                (
                    _clamp_morale(enemy.army_morale)
                    for f in enemy_factions
                    for enemy in sides.get(f, [])
                    if not enemy.is_garrison
                ),
                default=2,
            )
            morale_adv = 0 if army.is_garrison else max(0, _clamp_morale(army.army_morale) - highest_enemy_morale)
            is_attacker = army.army_id in attacker_ids
            active_kind = "attack" if is_attacker else (
                _active_non_attack_kind(
                    session,
                    int(army.commander_id) if army.commander_id is not None else -1,
                    exclude_action_ids=all_attack_action_ids,
                )
                if army.commander_id is not None
                else None
            )
            chosen_battlefield = 1 if (not is_attacker and active_kind is None) else 0
            undersupplied = 1 if int(army.army_supply or 0) < int(supply_stats(army).daily_consumption or 0) else 0
            out_of_formation = 1 if (
                army.army_id in forced_out_of_formation_army_ids
                or (active_kind is not None and active_kind != "attack")
            ) else 0
            target_h3 = None
            if is_attacker:
                for action_id in outgoing_action_ids_by_attacker.get(army.army_id, []):
                    if action_id in action_ids_in_component:
                        target_h3 = target_h3_by_action_id.get(action_id)
                        if target_h3:
                            break
            else:
                incoming = incoming_by_target.get(army.army_id, [])
                if incoming:
                    target_h3 = target_h3_by_action_id.get(incoming[0])
            rough_terrain = 0
            if target_h3 and engagement_type != "siege":
                terrain_row = (
                    session.query(TerrainType.terrain_name)
                    .join(Location, Location.terrain_id == TerrainType.terrain_id)
                    .filter(Location.location_id == target_h3)
                    .first()
                )
                terrain_name = str(terrain_row[0]).strip().lower() if terrain_row else ""
                if terrain_name and terrain_name != "open ground":
                    rough_terrain = 1
            surprise = 0
            if is_attacker and army.army_id not in disable_surprise_army_ids:
                target_army_id = None
                for action_id in outgoing_action_ids_by_attacker.get(army.army_id, []):
                    if action_id in action_ids_in_component:
                        target_army_id = target_army_id_by_action_id.get(action_id)
                        if target_army_id is not None:
                            break
                defender = session.get(Army, target_army_id) if target_army_id is not None else None
                if defender is not None:
                    radius = _environs_radius_for_army(defender)
                    try:
                        visible = set(h3.grid_disk(defender.location_id, radius))
                    except Exception:
                        visible = set()
                    if army.location_id not in visible:
                        surprise = 1
            mods = {
                "numerical_advantage": numerical_adv,
                "morale_advantage": morale_adv,
                "chosen_battlefield": chosen_battlefield,
                "surprise": surprise,
                "rough_terrain": -1 if rough_terrain else 0,
                "undersupplied": -1 if undersupplied else 0,
                "out_of_formation": -2 if out_of_formation else 0,
                "attacker_modifier": attacker_flat_modifier if is_attacker else 0,
                "defender_modifier": 0 if is_attacker else int(defender_flat_modifier_by_army_id.get(army.army_id, 0) or 0),
            }
            roll = random.randint(1, 6) + random.randint(1, 6)
            final_roll = roll + sum(mods.values())
            army_modifiers[army.army_id] = mods
            army_final_roll[army.army_id] = final_roll
            prior = side_top_roll.get(faction)
            if prior is None or final_roll > prior:
                side_top_roll[faction] = final_roll
                side_top_army_id[faction] = army.army_id

        top_score = max(side_top_roll.values())
        top_factions = [faction for faction, score in side_top_roll.items() if score == top_score]
        winner_faction = None if allow_side_draw and len(top_factions) > 1 else max(side_top_roll.keys(), key=lambda faction: side_top_roll[faction])
        winner_armies = sides.get(winner_faction, []) if winner_faction is not None else []
        winner_top_army = session.get(Army, side_top_army_id[winner_faction]) if winner_faction is not None and winner_faction in side_top_army_id else None

        casualties_by_army: dict[int, int] = {}
        morale_delta_by_army: dict[int, int] = defaultdict(int)
        retreat_by_army: dict[int, dict[str, Any]] = {}
        rout_by_army: dict[int, bool] = defaultdict(bool)
        supply_transfer_by_army: dict[int, dict[str, int]] = defaultdict(dict)

        for army in participant_armies:
            faction = str(army.army_faction)
            enemy_top = max((side_top_roll.get(f, -999) for f in side_top_roll.keys() if f != faction), default=-999)
            own_roll = army_final_roll.get(army.army_id, 0)
            diff = abs(own_roll - enemy_top)
            loser = own_roll < enemy_top
            winner = own_roll > enemy_top
            casualty_pct = 0.0
            if diff == 0:
                casualty_pct = 0.05
                if army.army_id in attacker_ids and not army.is_garrison:
                    morale_delta_by_army[army.army_id] -= 1
            elif diff == 1:
                casualty_pct = 0.10
                if loser and not army.is_garrison:
                    morale_delta_by_army[army.army_id] -= 1
            elif diff in {2, 3}:
                casualty_pct = 0.05 if winner else 0.10
                if loser and not army.is_garrison:
                    morale_delta_by_army[army.army_id] -= 2
                if winner and not army.is_garrison:
                    morale_delta_by_army[army.army_id] += 1
            elif diff in {4, 5}:
                casualty_pct = 0.05 if winner else 0.15
                if loser and not army.is_garrison:
                    morale_delta_by_army[army.army_id] -= 2
                if winner and not army.is_garrison:
                    morale_delta_by_army[army.army_id] += 2
            else:
                casualty_pct = 0.05 if winner else 0.20
                if loser and not army.is_garrison:
                    morale_delta_by_army[army.army_id] -= 2
                if winner and not army.is_garrison:
                    morale_delta_by_army[army.army_id] += 2
            if loser_extra_casualty_pct > 0.0 and loser:
                casualty_pct += max(0.0, float(loser_extra_casualty_pct))
            casualties_by_army[army.army_id] = _apply_random_warrior_loss(session, army, casualty_pct)

            if loser:
                if not attacker_can_retreat_or_rout and army.army_id in attacker_ids:
                    retreat_by_army[army.army_id] = {"retreated": False, "siege_attacker_held": True}
                elif engagement_type == "siege" and army.army_id not in attacker_ids:
                    retreat_info = _retreat_one_cell_with_wagon_drop(session, army=army, winner_armies=winner_armies, clock=clock)
                    retreat_by_army[army.army_id] = retreat_info
                elif battle_copy_mode == "sortie" and army.army_id in sortie_attacker_army_ids:
                    retreat_by_army[army.army_id] = {"retreated": True, "sortied_back": True}
                else:
                    retreat_ok = _retreat_one_cell(session, army=army, winner_armies=winner_armies, clock=clock)
                    retreat_by_army[army.army_id] = {"retreated": retreat_ok}
                    if not retreat_ok and army.is_garrison:
                        retreat_by_army[army.army_id] = {
                            "retreated": False,
                            "destroyed": True,
                            "garrison_destroyed": True,
                        }
                    elif not retreat_ok:
                        lost_w, lost_s = _halve_army(session, army)
                        retreat_by_army[army.army_id] = {
                            "retreated": False,
                            "fallback_halved": True,
                            "lost_warriors": lost_w,
                            "lost_supply": lost_s,
                        }
                    elif not army.is_garrison:
                        check = random.randint(1, 6) + random.randint(1, 6)
                        if check > _clamp_morale(army.army_morale):
                            rout_by_army[army.army_id] = True
                            supply_loss_pct = random.randint(1, 6) * 0.10
                            lost_supply = _apply_supply_loss(army, supply_loss_pct)
                            supply_transfer_by_army[army.army_id]["lost"] = int(lost_supply)
                            if winner_top_army is not None:
                                winner_top_army.army_supply = int(winner_top_army.army_supply or 0) + lost_supply
                                _clamp_army_supply_to_capacity(winner_top_army)
                                supply_transfer_by_army[winner_top_army.army_id]["looted"] = (
                                    int(supply_transfer_by_army[winner_top_army.army_id].get("looted", 0)) + int(lost_supply)
                                )
                            extra_steps = random.randint(1, 6)
                            path = _build_rout_path(
                                session,
                                army=army,
                                winner_armies=winner_armies,
                                start_h3=army.location_id,
                                steps=extra_steps,
                            )
                            if army.commander_id is not None:
                                _create_rout_action(
                                    session,
                                    army=army,
                                    commander_id=army.commander_id,
                                    clock=clock,
                                    path=path,
                                    source_battle={"component": comp_idx, "winner_faction": winner_faction},
                                )

        for army in participant_armies:
            _apply_morale_delta(army, morale_delta_by_army.get(army.army_id, 0))

        for action_id in action_ids_in_component:
            action = action_by_id.get(action_id)
            if action is None:
                continue
            winner_faction_by_action_id[action_id] = winner_faction
            if action.state == "in_progress":
                if winner_faction is not None and action_id in winner_destination_by_action_id:
                    acting_army = session.query(Army).filter(Army.commander_id == action.commander_id).first()
                    if acting_army is not None and str(acting_army.army_faction) == str(winner_faction):
                        _execute_move_to_destination(session, clock, acting_army, winner_destination_by_action_id[action_id])
                action.state = "completed"
                completed += 1

        battlefield_h3s = sorted(
            {
                str(target_h3_by_action_id[action_id])
                for action_id in action_ids_in_component
                if target_h3_by_action_id.get(action_id)
            }
        )
        participant_results = []
        participant_after = {
            army.army_id: army_history_payload(army)
            for army in sorted(participant_armies, key=lambda row: row.army_id)
        }
        for army_id in sorted(participant_before):
            before = participant_before[army_id]
            after = participant_after[army_id]
            casualties = int(casualties_by_army.get(army_id, 0) or 0)
            participant_results.append(
                {
                    "before": before,
                    "after": after,
                    "strength_before": int(before["strength"]),
                    "strength_after": int(after["strength"]),
                    "casualties": casualties,
                    "roll": int(army_final_roll.get(army_id, 0) or 0),
                    "morale_delta": int(morale_delta_by_army.get(army_id, 0) or 0),
                    "retreat": retreat_by_army.get(army_id, {}),
                    "routed": bool(rout_by_army.get(army_id, False)),
                }
            )
        battle_identity = json.dumps(
            {
                "tick": clock.world_tick,
                "mode": battle_copy_mode,
                "participants": sorted(participant_before),
                "locations": battlefield_h3s,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        record_history_event(
            session,
            event_key=f"battle:{clock.world_tick}:{hashlib.sha256(battle_identity.encode('utf-8')).hexdigest()[:32]}",
            world_tick=clock.world_tick,
            event_kind="battle",
            location_id=battlefield_h3s[0] if battlefield_h3s else participant_armies[0].location_id,
            payload={
                "battle_component_id": comp_idx,
                "mode": battle_copy_mode,
                "engagement_type": engagement_type,
                "location_h3s": battlefield_h3s,
                "participants": participant_results,
                "factions": sorted(sides),
                "winner_faction": winner_faction,
            },
        )

        for army in participant_armies:
            if army.commander_id is None:
                continue
            own_roll = army_final_roll.get(army.army_id, 0)
            enemy_top = max(
                (side_top_roll.get(f, -999) for f in side_top_roll.keys() if f != str(army.army_faction)),
                default=-999,
            )
            own_mods = army_modifiers.get(army.army_id, {})
            own_faction = str(army.army_faction)
            army_name = str(army.army_name or f"Army {army.army_id}")
            enemy_armies = [other for other in participant_armies if str(other.army_faction) != own_faction]
            enemy_primary = enemy_armies[0] if enemy_armies else None
            enemy_name = str(enemy_primary.army_name or "enemy army") if enemy_primary is not None else "enemy army"

            # If this army was an attacker, prefer naming the explicit target from submitted attack.
            if army.army_id in attacker_ids:
                for action_id in outgoing_action_ids_by_attacker.get(army.army_id, []):
                    if action_id in action_ids_in_component:
                        target_army_id = target_army_id_by_action_id.get(action_id)
                        target_army = session.get(Army, target_army_id) if target_army_id is not None else None
                        if target_army is not None:
                            enemy_name = str(target_army.army_name or enemy_name)
                            enemy_primary = target_army
                            break
            else:
                incoming = incoming_by_target.get(army.army_id, [])
                if incoming:
                    attacker_id = None
                    for inc_action_id in incoming:
                        for row_action_id, row_attacker_id, _ in edges:
                            if row_action_id == inc_action_id:
                                attacker_id = row_attacker_id
                                break
                        if attacker_id is not None:
                            break
                    attacker_army = session.get(Army, attacker_id) if attacker_id is not None else None
                    if attacker_army is not None:
                        enemy_name = str(attacker_army.army_name or enemy_name)
                        enemy_primary = attacker_army

            enemy_qualifiers: list[str] = []
            if enemy_primary is not None:
                enemy_mods = army_modifiers.get(enemy_primary.army_id, {})
                if int(enemy_mods.get("out_of_formation", 0) or 0) < 0:
                    enemy_qualifiers.append("out of formation")
                if int(enemy_mods.get("undersupplied", 0) or 0) < 0:
                    enemy_qualifiers.append("undersupplied")
            enemy_display = f"{enemy_name} ({', '.join(enemy_qualifiers)})" if enemy_qualifiers else enemy_name

            own_side_strength = max(1, int(side_strength.get(own_faction, 1)))
            enemy_side_strength = max(
                (int(side_strength.get(f, 1)) for f in side_strength.keys() if f != own_faction),
                default=1,
            )
            own_ratio = _ratio_label(own_side_strength, enemy_side_strength)
            enemy_ratio = _ratio_label(enemy_side_strength, own_side_strength)
            battle_position: list[str] = []
            if own_ratio:
                battle_position.append(f"with {own_ratio} numerical superiority")
            elif enemy_ratio:
                battle_position.append(f"outnumbered {enemy_ratio}")
            if battle_copy_mode != "siege_assault" and int(own_mods.get("chosen_battlefield", 0) or 0) > 0:
                battle_position.append("holding chosen ground")
            if int(own_mods.get("surprise", 0) or 0) > 0:
                battle_position.append("with surprise")
            if int(own_mods.get("rough_terrain", 0) or 0) < 0:
                battle_position.append("in rough terrain")
            if int(own_mods.get("undersupplied", 0) or 0) < 0:
                battle_position.append("while undersupplied")
            if int(own_mods.get("out_of_formation", 0) or 0) < 0:
                battle_position.append("out of formation")

            if battle_copy_mode == "meeting":
                opener = f"Meeting engagement with {enemy_display}"
            elif battle_copy_mode == "sortie":
                if army.army_id in sortie_attacker_army_ids:
                    opener = f"Sortied against {enemy_display}"
                else:
                    opener = f"Repelled sortie by {enemy_display}"
            elif battle_copy_mode == "siege_assault":
                if army.army_id in attacker_ids:
                    assault_target_name = enemy_display
                    for action_id in outgoing_action_ids_by_attacker.get(army.army_id, []):
                        if action_id in action_ids_in_component:
                            target_h3 = str(target_h3_by_action_id.get(action_id) or "").strip()
                            if target_h3:
                                target_stronghold = _stronghold_at_h3(session, target_h3)
                                if target_stronghold is not None:
                                    assault_target_name = target_stronghold.stronghold_name
                            break
                    opener = f"Assaulted {assault_target_name}"
                else:
                    opener = f"Repelled assault by {enemy_display}"
            elif army.army_id in attacker_ids:
                opener = f"Attacked {enemy_display}"
            else:
                opener = f"Attacked by {enemy_display}"
            if battle_position:
                opener = f"{opener} {' and '.join(battle_position)}"
            opener = f"BATTLE!\n{opener}."

            casualties = int(casualties_by_army.get(army.army_id, 0) or 0)
            enemies_slain = sum(int(casualties_by_army.get(other.army_id, 0) or 0) for other in enemy_armies)
            morale_delta = int(morale_delta_by_army.get(army.army_id, 0) or 0)
            if morale_delta > 0:
                morale_text = "army morale increased."
            elif morale_delta < 0:
                morale_text = "army morale decreased."
            else:
                morale_text = "army morale held."
            if winner_faction is None:
                outcome_text = "DRAW"
            else:
                outcome_text = "VICTORY" if own_faction == winner_faction else "DEFEAT"
            enemy_routed = any(bool(rout_by_army.get(other.army_id, False)) for other in enemy_armies)
            own_routed = bool(rout_by_army.get(army.army_id, False))
            own_supply_lost = int(supply_transfer_by_army.get(army.army_id, {}).get("lost", 0) or 0)
            own_supply_looted = int(supply_transfer_by_army.get(army.army_id, {}).get("looted", 0) or 0)
            supply_text = ""
            if own_supply_lost > 0:
                supply_text = f" {own_supply_lost} supply lost."
            elif own_supply_looted > 0:
                supply_text = f" {own_supply_looted} supply looted."
            if enemy_routed:
                rout_text = " Enemy routed."
            elif own_routed:
                rout_text = " Army routed."
            else:
                rout_text = ""
            wagon_text = " Forced to abandon wagons." if bool(retreat_by_army.get(army.army_id, {}).get("dropped_wagons")) else ""
            message = (
                f"{opener}\n{outcome_text}!\n"
                f"{casualties} warriors lost, {enemies_slain} enemies slain, {morale_text}{supply_text}{rout_text}{wagon_text}"
            )
            _create_alert(
                session,
                recipient_commander_id=army.commander_id,
                alert_type="violence",
                signal_kind="event",
                category="battle",
                importance=BATTLE_ALERT_IMPORTANCE,
                message=message,
                created_day=clock.day,
                created_watch=clock.watch,
                payload={
                    "battle_component_id": comp_idx,
                    "participant_army_ids": sorted(list(participant_ids)),
                    "winner_faction": winner_faction,
                    "own_roll": own_roll,
                    "enemy_top_roll": enemy_top,
                    "modifiers": own_mods,
                    "casualties": casualties_by_army.get(army.army_id, 0),
                    "enemy_casualties": enemies_slain,
                    "outcome": outcome_text.lower(),
                    "morale_delta": morale_delta_by_army.get(army.army_id, 0),
                    "supply_lost": own_supply_lost,
                    "supply_looted": own_supply_looted,
                    "retreat": retreat_by_army.get(army.army_id, {}),
                    "rout": bool(rout_by_army.get(army.army_id, False)),
                },
            )

    return {"completed": completed, "failed": failed, "winner_faction_by_action_id": winner_faction_by_action_id}


def _resolve_due_attack_battles(session: Session, clock: GameClock, due_attack_actions: list[Action]) -> dict[str, int]:
    if not due_attack_actions:
        return {"completed": 0, "failed": 0}

    action_by_id: dict[int, Action] = {action.action_id: action for action in due_attack_actions}
    target_army_id_by_action_id: dict[int, int] = {}
    target_h3_by_action_id: dict[int, str] = {}
    edges: list[tuple[int, int, int]] = []  # (action_id, attacker_army_id, target_army_id)
    failed = 0
    siege_action_ids: set[int] = set()
    sortie_action_ids: set[int] = set()
    sortie_defender_ids_by_stronghold_id: dict[int, set[int]] = defaultdict(set)
    sortie_siege_by_action_id: dict[int, Siege] = {}
    sortie_stronghold_by_action_id: dict[int, Stronghold] = {}
    defender_bonus_by_army_id: dict[int, int] = {}
    siege_by_action_id: dict[int, Siege] = {}
    stronghold_by_action_id: dict[int, Stronghold] = {}

    for action in due_attack_actions:
        attacker = session.query(Army).filter(Army.commander_id == action.commander_id).first()
        if attacker is None:
            continue
        occupied_stronghold, _, valid_stronghold_targets = _valid_stronghold_attack_targets(session, attacker)
        if occupied_stronghold is None:
            continue
        try:
            params = json.loads(action.parameters_json or "{}")
        except json.JSONDecodeError:
            params = {}
        raw_target_army_id = params.get("target_army_id")
        target_h3 = str(params.get("target_h3") or "").strip()
        try:
            target_army_id = int(raw_target_army_id)
        except (TypeError, ValueError):
            continue
        valid_target = next(
            (candidate for candidate in valid_stronghold_targets if candidate.army_id == target_army_id),
            None,
        )
        if valid_target is not None and valid_target.location_id == target_h3:
            sortie_action_ids.add(action.action_id)
            sortie_defender_ids_by_stronghold_id[occupied_stronghold.stronghold_id].add(attacker.army_id)

    for action in due_attack_actions:
        attacker = session.query(Army).filter(Army.commander_id == action.commander_id).first()
        if attacker is None:
            action.state = "failed"
            failed += 1
            continue
        is_sortie_action = action.action_id in sortie_action_ids
        if _army_is_in_stronghold(session, attacker) and not is_sortie_action:
            action.state = "failed"
            failed += 1
            continue
        try:
            params = json.loads(action.parameters_json or "{}")
        except json.JSONDecodeError:
            params = {}
        target_h3 = str(params.get("target_h3") or "").strip()
        raw_target_army_id = params.get("target_army_id")
        if target_h3 == "" or raw_target_army_id is None:
            action.state = "failed"
            failed += 1
            continue
        try:
            target_army_id = int(raw_target_army_id)
        except (TypeError, ValueError):
            action.state = "failed"
            failed += 1
            continue
        target = session.get(Army, target_army_id)
        if target is None or target.army_id == attacker.army_id or target.army_faction == attacker.army_faction:
            action.state = "failed"
            failed += 1
            continue
        active_siege = _active_siege_for_besieger(session, attacker.army_id)
        if is_sortie_action:
            target_army_id_by_action_id[action.action_id] = target_army_id
            target_h3_by_action_id[action.action_id] = target_h3
            edges.append((action.action_id, attacker.army_id, target_army_id))
            continue
        if active_siege is not None:
            stronghold = session.get(Stronghold, active_siege.stronghold_id)
            if stronghold is not None and stronghold.location_id == target_h3:
                defenders = _defender_armies_in_stronghold(session, stronghold, attacker.army_faction)
                defenders = [army for army in defenders if army.army_faction != attacker.army_faction]
                sortie_defender_ids = sortie_defender_ids_by_stronghold_id.get(stronghold.stronghold_id, set())
                if sortie_defender_ids:
                    target_army_id_by_action_id[action.action_id] = target_army_id
                    target_h3_by_action_id[action.action_id] = target_h3
                    sortie_siege_by_action_id[action.action_id] = active_siege
                    sortie_stronghold_by_action_id[action.action_id] = stronghold
                    for defender in defenders:
                        if defender.army_id in sortie_defender_ids:
                            edges.append((action.action_id, attacker.army_id, defender.army_id))
                    sortie_action_ids.add(action.action_id)
                    continue
                if target_army_id not in {army.army_id for army in defenders}:
                    action.state = "failed"
                    failed += 1
                    continue
                siege_action_ids.add(action.action_id)
                siege_by_action_id[action.action_id] = active_siege
                stronghold_by_action_id[action.action_id] = stronghold
                target_army_id_by_action_id[action.action_id] = target_army_id
                target_h3_by_action_id[action.action_id] = target_h3
                for defender in defenders:
                    edges.append((action.action_id, attacker.army_id, defender.army_id))
                    if not bool(active_siege.gates_open):
                        bonus = int(SIEGE_DEFENDER_BONUS_BY_TYPE.get(str(stronghold.stronghold_type or "").strip().lower(), 0))
                        defender_bonus_by_army_id[defender.army_id] = bonus
                continue
        target_army_id_by_action_id[action.action_id] = target_army_id
        target_h3_by_action_id[action.action_id] = target_h3
        edges.append((action.action_id, attacker.army_id, target_army_id))

    siege_edges = [edge for edge in edges if edge[0] in siege_action_ids]
    sortie_edges = [edge for edge in edges if edge[0] in sortie_action_ids]
    normal_edges = [edge for edge in edges if edge[0] not in siege_action_ids and edge[0] not in sortie_action_ids]
    completed = 0

    if normal_edges:
        result = _resolve_battles_from_edges(
            session,
            clock,
            action_by_id=action_by_id,
            edges=normal_edges,
            target_h3_by_action_id=target_h3_by_action_id,
            target_army_id_by_action_id=target_army_id_by_action_id,
        )
        completed += int(result.get("completed", 0))
        failed += int(result.get("failed", 0))

    if sortie_edges:
        sortie_action_id_set = {action_id for action_id, _, _ in sortie_edges}
        sortie_army_ids = {
            defender_id
            for defender_ids in sortie_defender_ids_by_stronghold_id.values()
            for defender_id in defender_ids
        }
        sortie_forced_out_of_formation_ids = set(sortie_army_ids)
        for _, _, target_id in sortie_edges:
            if target_id not in sortie_army_ids:
                sortie_forced_out_of_formation_ids.add(target_id)
        for _, attacker_id, _ in sortie_edges:
            if attacker_id in sortie_army_ids:
                continue
            sortie_forced_out_of_formation_ids.discard(attacker_id)
        sortie_result = _resolve_battles_from_edges(
            session,
            clock,
            action_by_id={action_id: action_by_id[action_id] for action_id in sortie_action_id_set if action_id in action_by_id},
            edges=sortie_edges,
            target_h3_by_action_id=target_h3_by_action_id,
            target_army_id_by_action_id=target_army_id_by_action_id,
            forced_out_of_formation_army_ids=sortie_forced_out_of_formation_ids,
            battle_copy_mode="sortie",
            engagement_type="field",
            sortie_attacker_army_ids=sortie_army_ids,
        )
        completed += int(sortie_result.get("completed", 0))
        failed += int(sortie_result.get("failed", 0))
        for action_id, siege in sortie_siege_by_action_id.items():
            stronghold = sortie_stronghold_by_action_id.get(action_id)
            action = action_by_id.get(action_id)
            if siege.state != "active" or stronghold is None or action is None:
                continue
            besieger = session.query(Army).filter(Army.commander_id == action.commander_id).first()
            if besieger is None:
                continue
            if _active_siege_participant_for_army(session, besieger.army_id) is None:
                continue
            try:
                still_adjacent = (
                    besieger.location_id == stronghold.location_id
                    or stronghold.location_id in set(h3.grid_ring(besieger.location_id, 1))
                )
            except Exception:
                still_adjacent = False
            if not still_adjacent:
                continue
            existing_besiege = (
                session.query(Action)
                .filter(
                    Action.commander_id == action.commander_id,
                    Action.kind == "besiege",
                    Action.state == "in_progress",
                )
                .first()
            )
            if existing_besiege is not None:
                continue
            session.add(
                Action(
                    commander_id=action.commander_id,
                    kind="besiege",
                    state="in_progress",
                    parameters_json=json.dumps(
                        {
                            "target_stronghold_id": stronghold.stronghold_id,
                            "target_h3": stronghold.location_id,
                            "target_stronghold_name": stronghold.stronghold_name,
                        }
                    ),
                    accepted_at=datetime.now(timezone.utc),
                    started_day=clock.day,
                    started_watch=clock.watch,
                    eta_day=None,
                    eta_watch=None,
                )
            )

    if siege_edges:
        siege_action_by_id = {action_id: action_by_id[action_id] for action_id in siege_action_ids if action_id in action_by_id}
        siege_result = _resolve_battles_from_edges(
            session,
            clock,
            action_by_id=siege_action_by_id,
            edges=siege_edges,
            target_h3_by_action_id=target_h3_by_action_id,
            target_army_id_by_action_id=target_army_id_by_action_id,
            battle_copy_mode="siege_assault",
            engagement_type="siege",
            attacker_flat_modifier=-1,
            defender_flat_modifier_by_army_id=defender_bonus_by_army_id,
            loser_extra_casualty_pct=0.10,
            attacker_can_retreat_or_rout=False,
        )
        completed += int(siege_result.get("completed", 0))
        failed += int(siege_result.get("failed", 0))
        winner_faction_by_action_id = siege_result.get("winner_faction_by_action_id", {})
        for action_id in siege_action_ids:
            action = action_by_id.get(action_id)
            siege = siege_by_action_id.get(action_id)
            stronghold = stronghold_by_action_id.get(action_id)
            attacker = session.query(Army).filter(Army.commander_id == action.commander_id).first() if action is not None else None
            if action is None or siege is None or stronghold is None or attacker is None:
                continue
            winner_faction = winner_faction_by_action_id.get(action_id)
            if winner_faction is not None and str(winner_faction) == str(attacker.army_faction):
                if not _clear_remaining_defenders_for_capture(
                    session,
                    clock=clock,
                    stronghold=stronghold,
                    attacker=attacker,
                ):
                    continue
                if not _finalize_siege_capture(
                    session,
                    clock=clock,
                    siege=siege,
                    stronghold=stronghold,
                    attacker=attacker,
                    apply_loot=True,
                ):
                    continue
            else:
                besiege_action = Action(
                    commander_id=action.commander_id,
                    kind="besiege",
                    state="in_progress",
                    parameters_json=json.dumps(
                        {
                            "target_stronghold_id": stronghold.stronghold_id,
                            "target_h3": stronghold.location_id,
                            "target_stronghold_name": stronghold.stronghold_name,
                        }
                    ),
                    accepted_at=datetime.now(timezone.utc),
                    started_day=clock.day,
                    started_watch=clock.watch,
                    eta_day=None,
                    eta_watch=None,
                )
                session.add(besiege_action)

    return {"completed": completed, "failed": failed}


def _process_sieges_matin_tick(session: Session, clock: GameClock) -> None:
    if clock.watch != int(Watch.MATIN):
        return
    _occupy_all_abandoned_sieged_strongholds(session, clock=clock)
    active_sieges = session.query(Siege).filter(Siege.state == "active").all()
    active_stronghold_ids = {int(siege.stronghold_id) for siege in active_sieges}
    for siege in active_sieges:
        stronghold = session.get(Stronghold, siege.stronghold_id)
        participants = _active_siege_participants_for_siege(session, siege)
        if stronghold is None or not participants:
            _end_siege(session, siege=siege, clock=clock, reason="besieger_destroyed")
            continue
        for participant in list(participants):
            besieger = session.get(Army, participant.besieger_army_id)
            if besieger is None:
                _remove_siege_participant(
                    session,
                    siege=siege,
                    army_id=participant.besieger_army_id,
                    clock=clock,
                    reason="besieger_destroyed",
                )
                continue
            still_adjacent = False
            try:
                still_adjacent = (
                    besieger.location_id == stronghold.location_id
                    or stronghold.location_id in set(h3.grid_ring(besieger.location_id, 1))
                )
            except Exception:
                still_adjacent = False
            if not still_adjacent:
                _remove_siege_participant(
                    session,
                    siege=siege,
                    army_id=participant.besieger_army_id,
                    clock=clock,
                    reason="besieger_displaced",
                )
        if siege.state != "active":
            continue
        besieger_faction = _active_siege_faction(session, siege)
        if not besieger_faction:
            _end_siege(session, siege=siege, clock=clock, reason="cancelled")
            continue
        defenders = _defender_armies_in_stronghold(session, stronghold, besieger_faction)
        if not defenders:
            if _occupy_abandoned_sieged_stronghold(session, clock=clock, siege=siege):
                continue
            _end_siege(session, siege=siege, clock=clock, reason="defender_absent")
            continue
        siege.current_resistance = max(0.0, float(siege.current_resistance or 0.0) - (1.0 / 7.0))
        siege.matin_ticks_elapsed = int(siege.matin_ticks_elapsed or 0) + 1
        if not bool(siege.gates_open):
            if random.random() < _siege_assault_probability_open(float(siege.current_resistance or 0.0)):
                siege.gates_open = True
                defender_commanders = [int(army.commander_id) for army in defenders if army.commander_id is not None]
                _emit_gates_open_alerts(
                    session,
                    siege=siege,
                    stronghold=stronghold,
                    defender_commanders=defender_commanders,
                    clock=clock,
                )

    strongholds = session.query(Stronghold).all()
    for stronghold in strongholds:
        if int(stronghold.stronghold_id) in active_stronghold_ids:
            continue
        max_resistance = _max_resistance_for_stronghold(stronghold)
        latest = (
            session.query(Siege)
            .filter(Siege.stronghold_id == stronghold.stronghold_id)
            .order_by(Siege.siege_id.desc())
            .first()
        )
        if latest is None:
            continue
        if float(latest.current_resistance or max_resistance) >= max_resistance:
            continue
        latest.current_resistance = min(max_resistance, float(latest.current_resistance or 0.0) + (1.0 / 7.0))
        latest.max_resistance = max_resistance
        latest.gates_open = False


def _occupy_abandoned_sieged_stronghold(session: Session, *, clock: GameClock, siege: Siege) -> bool:
    if siege.state != "active":
        return False
    stronghold = session.get(Stronghold, siege.stronghold_id)
    if stronghold is None:
        return False
    participants = _active_siege_participants_for_siege(session, siege)
    besieger = None
    for participant in participants:
        candidate = session.get(Army, participant.besieger_army_id)
        if candidate is None:
            continue
        try:
            adjacent = (
                candidate.location_id == stronghold.location_id
                or stronghold.location_id in set(h3.grid_ring(candidate.location_id, 1))
            )
        except Exception:
            adjacent = False
        if adjacent:
            besieger = candidate
            break
    if besieger is None:
        return False
    defenders = _defender_armies_in_stronghold(session, stronghold, besieger.army_faction)
    if defenders:
        return False
    if not _clear_remaining_defenders_for_capture(
        session,
        clock=clock,
        stronghold=stronghold,
        attacker=besieger,
    ):
        return False
    return _finalize_siege_capture(
        session,
        clock=clock,
        siege=siege,
        stronghold=stronghold,
        attacker=besieger,
        apply_loot=False,
    )


def _occupy_all_abandoned_sieged_strongholds(session: Session, *, clock: GameClock) -> None:
    session.flush()
    while True:
        changed = False
        active_sieges = session.query(Siege).filter(Siege.state == "active").all()
        for siege in active_sieges:
            if _occupy_abandoned_sieged_stronghold(session, clock=clock, siege=siege):
                changed = True
                session.flush()
        if not changed:
            break


def _clear_remaining_defenders_for_capture(
    session: Session,
    *,
    clock: GameClock,
    stronghold: Stronghold,
    attacker: Army,
) -> bool:
    remaining_defenders = _capture_blockers_in_stronghold(session, stronghold, attacker.army_faction)
    for defender in remaining_defenders:
        if _army_has_live_detachments(defender):
            result = _retreat_one_cell_with_wagon_drop(
                session,
                army=defender,
                winner_armies=[attacker],
                clock=clock,
            )
            if not result.get("retreated") and not result.get("destroyed"):
                return False
        elif not _retreat_one_cell(
            session,
            army=defender,
            winner_armies=[attacker],
            clock=clock,
        ):
            # Until zero-strength army lifecycle rules are defined, preserve the
            # commander's army and refuse an overlapping capture if it cannot be
            # displaced safely.
            return False
    session.flush()
    blockers = _capture_blockers_in_stronghold(session, stronghold, attacker.army_faction)
    return not blockers


def _finalize_siege_capture(
    session: Session,
    *,
    clock: GameClock,
    siege: Siege,
    stronghold: Stronghold,
    attacker: Army,
    apply_loot: bool,
) -> bool:
    blockers = _capture_blockers_in_stronghold(session, stronghold, attacker.army_faction)
    if blockers:
        return False

    destination_location = session.get(Location, stronghold.location_id)
    if destination_location is None:
        return False

    if attacker.location_id != stronghold.location_id:
        attacker.location = destination_location
        attacker.location_id = stronghold.location_id
        session.add(
            Movement(
                army_id=attacker.army_id,
                location_id=stronghold.location_id,
                date=_scenario_date_for_day(clock.day),
                watch=clock.watch,
            )
        )

    if stronghold.control != attacker.army_faction:
        _set_stronghold_control(session, stronghold=stronghold, new_faction=attacker.army_faction, clock=clock)
    else:
        garrison = _garrison_for_stronghold(session, stronghold)
        if garrison is not None:
            garrison.army_faction = attacker.army_faction
            garrison.location_id = stronghold.location_id
            garrison.location = stronghold.location

    if apply_loot:
        siege_length = max(0, int(siege.matin_ticks_elapsed or 0))
        loot_roll = random.randint(1, 6)
        loot_scale = int(SIEGE_LOOT_SCALE_BY_TYPE.get(str(stronghold.stronghold_type or "").strip().lower(), 0))
        looted_supply = max(0, int((loot_roll - siege_length) * loot_scale))
        attacker.army_supply = int(attacker.army_supply or 0) + looted_supply
        _clamp_army_supply_to_capacity(attacker)
        attacker.army_morale = _clamp_morale(int(attacker.army_morale or 0) + 2)
        nc_gain = float(SIEGE_NONCOMBATANT_GAIN_BY_TYPE.get(str(stronghold.stronghold_type or "").strip().lower(), 0.0))
        attacker.noncombattant_percent = max(0.0, float(attacker.noncombattant_percent or 0.0) + nc_gain)
        _create_alert(
            session,
            recipient_commander_id=attacker.commander_id,
            alert_type="action",
            signal_kind="event",
            category="siege",
            importance="normal",
            message=(
                f"{stronghold.stronghold_name} looted: {looted_supply} supply taken, "
                f"{int(round(nc_gain * 100))}% more noncombatants gained, morale increased."
            ),
            created_day=clock.day,
            created_watch=clock.watch,
            payload={
                "stronghold_id": _stronghold_ref(stronghold.stronghold_id),
                "looted_supply": looted_supply,
                "noncombatant_percent_gain": nc_gain,
            },
        )

    _end_siege(session, siege=siege, clock=clock, reason="captured", emit_lift_alert=False)
    return True


def _start_action_now_if_valid(session: Session, action: Action, army: Army, clock: GameClock) -> bool:
    if action.kind == "move":
        if clock.watch == int(Watch.NIGHT) or _long_column_start_blocked(army, int(clock.watch)):
            # Movement does not start at night.
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
        forced_march = _forced_march_enabled_for_army(session, army)
        remaining_budget = _remaining_day_movement_budget_for_watch(int(clock.watch), army, forced_march)
        if watches_needed > remaining_budget:
            return False
        return _initialize_move_action_progress(
            session,
            action,
            army,
            start_day=clock.day,
            start_watch=clock.watch,
        )

    if action.kind == "forage":
        if _army_is_under_siege(session, army):
            action.state = "failed"
            return False
        if clock.watch == int(Watch.NIGHT):
            # Night submissions remain queued until at least Matin.
            return False
        if _army_has_long_column(army):
            if int(clock.watch) not in {int(Watch.PRIME), int(Watch.NOON)}:
                return False
            action.started_day = clock.day
            action.started_watch = clock.watch
            action.state = "in_progress"
            action.eta_day, action.eta_watch = _advance_day_watch(
                clock.day,
                clock.watch,
                2 if int(clock.watch) == int(Watch.PRIME) else 1,
            )
            return True
        # If execution skipped over Matin for any reason, start forage as-if at Matin.
        effective_start_watch = int(Watch.MATIN)
        action.started_day = clock.day
        action.started_watch = effective_start_watch
        action.state = "in_progress"
        # Forage duration is exactly 4 watch transitions from Matin start.
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, effective_start_watch, 4)
        return True

    if action.kind == "attack":
        if clock.watch == int(Watch.NIGHT) or _long_column_start_blocked(army, int(clock.watch)):
            return False
        try:
            payload = json.loads(action.parameters_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        target_h3 = str(payload.get("target_h3") or "").strip()
        target_army_id = payload.get("target_army_id")
        if target_h3 == "" or target_army_id is None:
            action.state = "failed"
            return False
        if _army_is_in_stronghold(session, army):
            try:
                parsed_target_army_id = int(target_army_id)
            except (TypeError, ValueError):
                action.state = "failed"
                return False
            _, _, valid_targets = _valid_stronghold_attack_targets(session, army)
            valid_target = next(
                (candidate for candidate in valid_targets if candidate.army_id == parsed_target_army_id),
                None,
            )
            if valid_target is None or valid_target.location_id != target_h3:
                action.state = "failed"
                return False
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, clock.watch, 1)
        return True

    if action.kind == "besiege":
        if _long_column_start_blocked(army, int(clock.watch)):
            return False
        try:
            payload = json.loads(action.parameters_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        target_h3 = str(payload.get("target_h3") or "").strip()
        target_stronghold_id = payload.get("target_stronghold_id")
        if not target_h3 or target_stronghold_id is None:
            action.state = "failed"
            return False
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        action.eta_day = None
        action.eta_watch = None
        return True

    if action.kind == "rout":
        try:
            payload = json.loads(action.parameters_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        path = [str(h3_index).strip() for h3_index in (payload.get("path") or []) if str(h3_index).strip()]
        if not path:
            action.state = "failed"
            return False
        watches_needed = _path_watches_for_army(session, army, army.location_id, path)
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, clock.watch, watches_needed)
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

    due_attack_actions: list[Action] = []

    def resolve_move_batch(move_batch: list[tuple[Action, Army, str]]) -> dict[str, int]:
        batch_completed = 0
        batch_failed = 0
        if not move_batch:
            return {"completed": 0, "failed": 0}

        by_destination: dict[str, list[tuple[Action, Army, str]]] = defaultdict(list)
        for row in move_batch:
            by_destination[row[2]].append(row)

        contested_battle_input: list[tuple[str, list[tuple[Action, Army, str]]]] = []
        uncontested_moves: list[tuple[Action, Army, str]] = []
        for destination_h3, rows in by_destination.items():
            factions = {str(army.army_faction) for _, army, _ in rows}
            if len(factions) > 1:
                contested_battle_input.append((destination_h3, rows))
            else:
                uncontested_moves.extend(rows)

        for action, army, destination_h3 in uncontested_moves:
            if action.state != "in_progress":
                continue
            if not _execute_move_to_destination(session, clock, army, destination_h3):
                action.state = "failed"
                batch_failed += 1
                continue
            action.state = "completed"
            batch_completed += 1

        synthetic_action_id = -1
        synthetic_actions: dict[int, Action] = {}
        synthetic_edges: list[tuple[int, int, int]] = []
        synthetic_target_h3_by_action_id: dict[int, str] = {}
        synthetic_target_army_id_by_action_id: dict[int, int] = {}
        synthetic_winner_destination_by_action_id: dict[int, str] = {}
        forced_out_of_formation_army_ids: set[int] = set()
        disable_surprise_army_ids: set[int] = set()

        for destination_h3, rows in contested_battle_input:
            for idx, (action, army, _) in enumerate(rows):
                forced_out_of_formation_army_ids.add(army.army_id)
                disable_surprise_army_ids.add(army.army_id)
                synthetic_actions[action.action_id] = action
                synthetic_winner_destination_by_action_id[action.action_id] = destination_h3
                opponents = [other_army for _, other_army, _ in rows if other_army.army_id != army.army_id and other_army.army_faction != army.army_faction]
                if not opponents:
                    continue
                for opponent in opponents:
                    synthetic_edges.append((action.action_id, army.army_id, opponent.army_id))
                    synthetic_target_h3_by_action_id[action.action_id] = destination_h3
                    synthetic_target_army_id_by_action_id[action.action_id] = opponent.army_id

        if synthetic_edges:
            battle_result = _resolve_battles_from_edges(
                session,
                clock,
                action_by_id=synthetic_actions,
                edges=synthetic_edges,
                target_h3_by_action_id=synthetic_target_h3_by_action_id,
                target_army_id_by_action_id=synthetic_target_army_id_by_action_id,
                forced_out_of_formation_army_ids=forced_out_of_formation_army_ids,
                disable_surprise_army_ids=disable_surprise_army_ids,
                allow_side_draw=True,
                winner_destination_by_action_id=synthetic_winner_destination_by_action_id,
                battle_copy_mode="meeting",
            )
            batch_completed += int(battle_result.get("completed", 0))
            batch_failed += int(battle_result.get("failed", 0))

        return {"completed": batch_completed, "failed": batch_failed}

    # First, attempt to complete currently in-progress non-attack actions.
    for commander_id, commander_actions in in_progress_by_commander.items():
        action = commander_actions[0]
        army = session.query(Army).filter(Army.commander_id == action.commander_id).first()
        if army is None:
            action.state = "failed"
            failed += 1
            continue
        if action.kind in {"move", "besiege"}:
            continue
        if _long_column_completion_blocked(army, int(clock.watch)):
            continue
        if action.eta_day is None or action.eta_watch is None:
            action.state = "failed"
            failed += 1
            continue
        if not _watch_is_at_or_after(clock.day, clock.watch, action.eta_day, action.eta_watch):
            continue

        if action.kind == "attack":
            due_attack_actions.append(action)
            continue

        if action.kind == "forage":
            # Revalidate at completion: a queued or underway forage order may
            # have become trapped inside a newly established enemy siege.
            if _army_is_under_siege(session, army):
                action.state = "failed"
                failed += 1
                continue
            gain, forageable_locations, average_depletion = _forage_supply_gain_for_army(session, army)
            capacity = supply_stats(army).capacity
            supply_before = int(army.army_supply or 0)
            army.army_supply = min(capacity, army.army_supply + gain)
            applied_gain = max(0, int(army.army_supply or 0) - supply_before)
            for location in forageable_locations:
                location.foraged_this_season = min(3, _forage_depletion_level(location) + 1)
            action.state = "completed"
            _restore_besiege_action_for_active_participant(
                session,
                army=army,
                commander_id=action.commander_id,
                clock=clock,
            )
            forage_condition = _forage_condition_word(average_depletion)
            _create_alert(
                session,
                recipient_commander_id=action.commander_id,
                alert_type="action",
                signal_kind="event",
                category="action",
                importance="normal",
                message=f"{applied_gain} supply foraged from {forage_condition} country",
                created_day=clock.day,
                created_watch=clock.watch,
            )
            completed += 1
            continue

        if action.kind == "rout":
            try:
                payload = json.loads(action.parameters_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            path = [str(h3_index).strip() for h3_index in (payload.get("path") or []) if str(h3_index).strip()]
            source_battle = payload.get("source_battle") if isinstance(payload.get("source_battle"), dict) else {}
            if not path:
                action.state = "failed"
                failed += 1
                continue
            destination_h3 = path[0]
            if not _execute_move_to_destination(session, clock, army, destination_h3):
                action.state = "failed"
                failed += 1
                continue
            remaining_path = path[1:]
            if remaining_path:
                try:
                    _schedule_next_rout_leg(
                        session,
                        action=action,
                        army=army,
                        clock=clock,
                        remaining_path=remaining_path,
                        source_battle=source_battle,
                    )
                except ValueError:
                    action.state = "failed"
                    failed += 1
                    continue
            else:
                action.state = "completed"
                _create_alert(
                    session,
                    recipient_commander_id=action.commander_id,
                    alert_type="report",
                    signal_kind="event",
                    category="battle",
                    importance="normal",
                    message="Army rallied",
                    created_day=clock.day,
                    created_watch=clock.watch,
                )
                completed += 1
            continue

        action.state = "failed"
        failed += 1

    interval_start_day, interval_start_watch = _watch_interval_start_stamp_for_current_watch(clock.day, clock.watch)
    commander_ids = set(in_progress_by_commander.keys()) | set(queued_by_commander.keys())
    army_by_commander: dict[int, Army | None] = {
        commander_id: session.query(Army).filter(Army.commander_id == commander_id).first()
        for commander_id in commander_ids
    }
    move_failed_counter = [0]

    def _next_move_action_for_commander(commander_id: int, army: Army) -> tuple[Action | None, int, int]:
        local_started = 0
        local_completed = 0
        while True:
            current_rows = [
                row
                for row in in_progress_by_commander.get(commander_id, [])
                if row.state == "in_progress"
            ]
            if current_rows:
                current = current_rows[0]
                in_progress_by_commander[commander_id] = [current]
                if current.kind == "move":
                    return current, local_started, local_completed
                return None, local_started, local_completed

            queued = [
                row
                for row in sorted(
                    queued_by_commander.get(commander_id, []),
                    key=lambda a: (a.accepted_at, a.action_id),
                )
                if row.state == "queued"
            ]
            if not queued:
                return None, local_started, local_completed
            next_action = queued[0]
            if next_action.kind != "move":
                return None, local_started, local_completed
            if not _initialize_move_action_progress(
                session,
                next_action,
                army,
                start_day=interval_start_day,
                start_watch=interval_start_watch,
            ):
                if next_action.state == "failed":
                    move_failed_counter[0] += 1
                    return None, local_started, local_completed
                if next_action.state == "completed":
                    local_completed += 1
                    continue
                return None, local_started, local_completed
            if next_action.state == "completed":
                local_completed += 1
                continue
            in_progress_by_commander[commander_id] = [next_action]
            local_started += 1
            return next_action, local_started, local_completed

    move_interval_capacity: dict[int, int] = {}
    for commander_id, army in army_by_commander.items():
        if army is None:
            continue
        forced_march = _forced_march_enabled_for_army(session, army)
        move_interval_capacity[commander_id] = _movement_capacity_for_interval_start(
            interval_start_watch,
            army,
            forced_march,
        )

    max_subslots = max(move_interval_capacity.values(), default=0)
    blocked_move_commanders: set[int] = set()
    for _ in range(max_subslots):
        move_batch: list[tuple[Action, Army, str]] = []
        for commander_id in sorted(commander_ids):
            if commander_id in blocked_move_commanders:
                continue
            army = army_by_commander.get(commander_id)
            if army is None:
                continue
            if move_interval_capacity.get(commander_id, 0) <= 0:
                continue
            existing_in_progress = [
                row
                for row in in_progress_by_commander.get(commander_id, [])
                if row.state == "in_progress"
            ]
            if existing_in_progress and existing_in_progress[0].kind != "move":
                continue
            move_action, started_delta, completed_delta = _next_move_action_for_commander(commander_id, army)
            started += started_delta
            completed += completed_delta
            if move_action is None:
                continue
            remaining_cost = _move_remaining_cost(move_action)
            if remaining_cost is None:
                destination_h3 = _get_destination_h3(move_action)
                if destination_h3 is None:
                    move_action.state = "failed"
                    failed += 1
                    blocked_move_commanders.add(commander_id)
                    continue
                try:
                    remaining_cost = calculate_move_watches(session, army.army_id, destination_h3)
                except ValueError:
                    move_action.state = "failed"
                    failed += 1
                    blocked_move_commanders.add(commander_id)
                    continue
                _set_move_remaining_cost(move_action, remaining_cost)
            # Consuming the first movement progress is the effective departure
            # from siege duty, even when difficult terrain delays arrival.
            _abandon_active_siege_for_army(session, army=army, clock=clock)
            remaining_cost -= 1
            _set_move_remaining_cost(move_action, remaining_cost)
            move_interval_capacity[commander_id] = max(0, move_interval_capacity.get(commander_id, 0) - 1)
            if remaining_cost > 0:
                _refresh_move_eta(session, move_action, army, day=clock.day, watch=clock.watch)
                continue
            destination_h3 = _get_destination_h3(move_action)
            if destination_h3 is None:
                move_action.state = "failed"
                failed += 1
                blocked_move_commanders.add(commander_id)
                continue
            move_batch.append((move_action, army, destination_h3))

        if not move_batch:
            continue
        move_result = resolve_move_batch(move_batch)
        completed += int(move_result.get("completed", 0))
        failed += int(move_result.get("failed", 0))
        for action, army, _ in move_batch:
            commander_id = int(action.commander_id)
            if action.state != "in_progress":
                in_progress_by_commander[commander_id] = []
            if action.state == "failed":
                blocked_move_commanders.add(commander_id)
        _occupy_all_abandoned_sieged_strongholds(session, clock=clock)

    failed += move_failed_counter[0]

    # Resolve due attack battles after non-attack movement/forage/rout effects.
    battle_result = _resolve_due_attack_battles(session, clock, due_attack_actions)
    completed += int(battle_result.get("completed", 0))
    failed += int(battle_result.get("failed", 0))
    _occupy_all_abandoned_sieged_strongholds(session, clock=clock)

    # Then, promote queued actions when no in-progress action remains.
    commander_ids = set(in_progress_by_commander.keys()) | set(queued_by_commander.keys())
    pending_besiege_actions = []
    for commander_id, queued_actions in queued_by_commander.items():
        if any(action.state == "in_progress" for action in in_progress_by_commander.get(commander_id, [])):
            continue
        active_queue = sorted(
            (action for action in queued_actions if action.state == "queued"),
            key=lambda action: (action.accepted_at, action.action_id),
        )
        if active_queue and active_queue[0].kind == "besiege":
            pending_besiege_actions.append(active_queue[0])
    pending_besiege_actions.sort(key=lambda action: (action.accepted_at, action.action_id))
    for action in pending_besiege_actions:
        army = session.query(Army).filter(Army.commander_id == action.commander_id).first()
        if army is None:
            action.state = "failed"
            failed += 1
            continue
        if not _start_action_now_if_valid(session, action, army, clock):
            if action.state != "queued":
                failed += 1
            continue
        if not _activate_besiege_action_at_transition(
            session,
            action=action,
            army=army,
            clock=clock,
        ):
            failed += 1
            continue
        in_progress_by_commander[action.commander_id] = [action]
        started += 1

    for commander_id in commander_ids:
        has_in_progress = any(
            action.state == "in_progress" for action in in_progress_by_commander.get(commander_id, [])
        )
        if has_in_progress:
            continue
        queued = [
            action
            for action in queued_by_commander.get(commander_id, [])
            if action.state == "queued"
        ]
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
                    # Watch-restricted starts leave the action queued; do not advance to next queued action.
                    break
                failed += 1
                continue
            if action.kind == "besiege" and not _activate_besiege_action_at_transition(
                session,
                action=action,
                army=army,
                clock=clock,
            ):
                failed += 1
                continue
            if action.state == "completed":
                completed += 1
                continue
            started += 1
            break

    return {"started": started, "completed": completed, "failed": failed}


def _get_current_commander_id(
    authorization: str = Header(default=""),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    session: Session = Depends(_get_session),
) -> int:
    raw_token = ""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            raw_token = token.strip().strip("\"")
    if not raw_token and isinstance(session_cookie, str) and session_cookie:
        raw_token = session_cookie.strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    auth_token = session.get(AuthToken, _token_hash(raw_token))
    if auth_token is None or auth_token.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid token")
    now = datetime.now(timezone.utc)
    last_used = auth_token.last_used_at
    if last_used is None or (now.replace(tzinfo=None) - last_used.replace(tzinfo=None)).total_seconds() >= 300:
        auth_token.last_used_at = now
        session.commit()
    return auth_token.commander_id


def _find_commander_army(session: Session, commander_id: int) -> Army:
    army = session.query(Army).filter(Army.commander_id == commander_id).first()
    if army is None:
        raise HTTPException(status_code=404, detail="No army found for commander")
    return army


def _lock_commander_scope(session: Session, commander_id: int) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    session.query(Commander.commander_id).filter(Commander.commander_id == commander_id).with_for_update().one()
    session.query(Army.army_id).filter(Army.commander_id == commander_id).order_by(Army.army_id).with_for_update().all()
    session.query(Action.action_id).filter(
        Action.commander_id == commander_id,
        Action.state.in_(ACTIVE_ACTION_STATES),
    ).order_by(Action.action_id).with_for_update().all()


def _clamp_morale(value: int | None, default: int = 9) -> int:
    if value is None:
        return default
    return max(2, min(12, int(value)))


def _detachment_display_type(detachment: Any) -> str:
    special_names = sorted(
        str(special.special_name).strip()
        for special in list(getattr(detachment, "specials", []) or [])
        if str(getattr(special, "special_name", "")).strip()
    )
    if special_names:
        return special_names[0].lower()
    return ("heavy " if detachment.is_heavy else "light ") + (
        "cavalry" if detachment.is_cavalry else "infantry"
    )


def _serialize_army(army: Army) -> dict[str, Any]:
    stats = supply_stats(army)
    noncombatants = noncombatant_count(army)
    infantry = sum(int(det.warrior_count or 0) for det in army.detachments if not det.is_cavalry)
    cavalry = sum(int(det.warrior_count or 0) for det in army.detachments if det.is_cavalry)
    wagons = sum(int(det.wagon_count or 0) for det in army.detachments)
    column_length = _army_column_length(army)
    current_morale = _clamp_morale(army.army_morale)
    resting_morale = _clamp_morale(army.army_resting_morale, default=current_morale)

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
        "faction": army.army_faction,
        "location": {"h3": army.location_id},
        "composition": {
            "detachments": [
                {
                    "id": _detachment_ref(det.detachment_id),
                    "name": det.detachment_name,
                    "warriors": det.warrior_count,
                    "wagons": det.wagon_count,
                    "is_cavalry": det.is_cavalry,
                    "is_heavy": det.is_heavy,
                    "type": _detachment_display_type(det),
                }
                for det in army.detachments
            ],
            "noncombatants": noncombatants,
            "noncombatant_percent": float(army.noncombattant_percent or 0.0),
        },
        "supply": {
            "current": army.army_supply,
            "capacity": stats.capacity,
            "daily_consumption": stats.daily_consumption,
            "days_estimate": stats.days_estimate,
        },
        "column_length": column_length,
        "morale": {
            "current": current_morale,
            "resting": resting_morale,
            "min": 2,
            "max": 12,
        },
        "status_flags": status_flags,
    }


def _serialize_management_commander(commander: Commander | None) -> dict[str, Any] | None:
    if commander is None:
        return None
    return {
        "id": _commander_ref(commander.commander_id),
        "name": commander.commander_name,
        "title": commander.commander_title,
        "display_name": _commander_display_name(commander),
    }


def _load_army_management_templates() -> dict[str, Any]:
    if not ARMY_MANAGEMENT_TEMPLATE_PATH.exists():
        return {}
    try:
        return json.loads(ARMY_MANAGEMENT_TEMPLATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _random_unused_template_option(options: list[str], used_values: set[str]) -> str:
    available = [str(value).strip() for value in options if str(value).strip() and str(value).strip().lower() not in used_values]
    if not available:
        return ""
    return random.choice(available)


def _army_management_new_army_template(session: Session, faction: str) -> dict[str, Any]:
    templates = _load_army_management_templates()
    faction_templates = templates.get(str(faction or "").strip(), {}) if isinstance(templates, dict) else {}
    commander_titles = list(faction_templates.get("commander_titles") or [])
    commander_names = list(faction_templates.get("commander_names") or [])
    army_names = list(faction_templates.get("army_names") or [])
    used_commander_names = {
        str(row[0]).strip().lower()
        for row in session.query(Commander.commander_name).all()
        if str(row[0] or "").strip()
    }
    used_army_names = {
        str(row[0]).strip().lower()
        for row in session.query(Army.army_name).all()
        if str(row[0] or "").strip()
    }
    title = random.choice([str(value).strip() for value in commander_titles if str(value).strip()]) if commander_titles else ""
    return {
        "default_name": _random_unused_template_option(army_names, used_army_names),
        "default_commander_name": _random_unused_template_option(commander_names, used_commander_names),
        "default_commander_title": title,
        "supply": {"current": 0, "capacity": 0, "daily_consumption": 0, "days_estimate": None},
        "detachments": [],
    }


def _serialize_management_army(army: Army) -> dict[str, Any]:
    supply_payload = None
    if not army.is_garrison:
        stats = supply_stats(army)
        supply_payload = {
            "current": int(army.army_supply or 0),
            "capacity": int(stats.capacity or 0),
            "daily_consumption": int(stats.daily_consumption or 0),
            "days_estimate": stats.days_estimate,
        }
    return {
        "army_id": _army_ref(army.army_id),
        "name": army.army_name,
        "location_h3": army.location_id,
        "faction": army.army_faction,
        "is_garrison": bool(army.is_garrison),
        "commander": _serialize_management_commander(army.commander),
        "commander_id": _commander_ref(army.commander_id) if army.commander_id is not None else None,
        "supply": supply_payload,
        "noncombatant_percent": float(army.noncombattant_percent or 0.0),
        "morale": {
            "current": _clamp_morale(army.army_morale),
            "resting": _clamp_morale(army.army_resting_morale, default=_clamp_morale(army.army_morale)),
        },
        "detachments": [
            {
                "id": _detachment_ref(det.detachment_id),
                "name": det.detachment_name,
                "warriors": int(det.warrior_count or 0),
                "wagons": int(det.wagon_count or 0),
                "is_cavalry": bool(det.is_cavalry),
                "is_heavy": bool(det.is_heavy),
                "type": _detachment_display_type(det),
            }
            for det in sorted(army.detachments, key=lambda row: row.detachment_id)
        ],
    }


def _eligible_management_armies(session: Session, left_army: Army) -> list[Army]:
    return (
        session.query(Army)
        .options(
            joinedload(Army.commander),
            joinedload(Army.detachments).joinedload(Detachment.specials),
        )
        .filter(
            Army.location_id == left_army.location_id,
            Army.army_faction == left_army.army_faction,
            Army.army_id != left_army.army_id,
        )
        .order_by(Army.is_garrison.asc(), Army.army_name.asc(), Army.army_id.asc())
        .all()
    )


def _army_management_snapshot_hash(left_army: Army, candidates: list[Army]) -> str:
    snapshot = {
        "left_army_id": int(left_army.army_id),
        "location_h3": str(left_army.location_id or ""),
        "faction": str(left_army.army_faction or ""),
        "left": {
            "name": str(left_army.army_name or ""),
            "commander_id": int(left_army.commander_id) if left_army.commander_id is not None else None,
            "supply": int(left_army.army_supply or 0),
            "detachments": sorted(int(det.detachment_id) for det in left_army.detachments),
        },
        "candidates": [
            {
                "army_id": int(army.army_id),
                "name": str(army.army_name or ""),
                "commander_id": int(army.commander_id) if army.commander_id is not None else None,
                "supply": None if army.is_garrison else int(army.army_supply or 0),
                "detachments": sorted(int(det.detachment_id) for det in army.detachments),
                "is_garrison": bool(army.is_garrison),
            }
            for army in sorted(candidates, key=lambda row: row.army_id)
        ],
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()


def _army_is_routing(session: Session, army: Army | None) -> bool:
    if army is None or army.commander_id is None:
        return False
    current_action = _get_current_action_row(session, army.commander_id)
    return current_action is not None and current_action.state == "in_progress" and current_action.kind == "rout"


def _commander_has_active_siege(session: Session, commander_id: int | None) -> bool:
    if commander_id is None:
        return False
    return _find_active_siege_for_commander(session, commander_id) is not None


def _cancel_non_siege_actions_for_commander(session: Session, commander_id: int | None) -> dict[str, Any]:
    if commander_id is None:
        return {"cancelled": 0, "kinds": {}}
    rows = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )
    cancelled = 0
    kinds: dict[str, int] = {}
    for row in rows:
        if str(row.kind or "").strip().lower() == "besiege":
            continue
        row.state = "cancelled"
        kind = str(row.kind or "unknown").strip().lower() or "unknown"
        kinds[kind] = kinds.get(kind, 0) + 1
        cancelled += 1
    return {"cancelled": cancelled, "kinds": kinds}


def _army_management_error(message: str, *, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def _serialize_environs(
    session: Session,
    center_h3: str,
    radius: int,
    exclude_army_id: int | None = None,
    viewer_commander_id: int | None = None,
    viewer_army: Army | None = None,
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
    active_sieges_by_stronghold_id = {
        int(siege.stronghold_id): siege
        for siege in session.query(Siege).filter(Siege.state == "active").all()
    }
    armies_in_disk = (
        session.query(Army)
        .options(joinedload(Army.detachments), joinedload(Army.commander))
        .filter(Army.location_id.in_(disk))
        .order_by(Army.army_id.asc())
        .all()
    )
    other_armies_by_location: dict[str, list[dict[str, Any]]] = {}
    armies_by_location: dict[str, list[Army]] = {}
    for located_army in armies_in_disk:
        armies_by_location.setdefault(located_army.location_id, []).append(located_army)
        if located_army.is_garrison:
            continue
        if exclude_army_id is not None and located_army.army_id == exclude_army_id:
            continue
        other_army = located_army
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
                        _commander_display_name(other_army.commander)
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
        siege = active_sieges_by_stronghold_id.get(int(stronghold.stronghold_id)) if stronghold else None
        other_armies = other_armies_by_location.get(location.location_id, [])
        located_armies = armies_by_location.get(location.location_id, [])
        stronghold_name = stronghold.stronghold_name if stronghold else None
        terrain_type = terrain.terrain_name if terrain else "unknown"
        siege_payload = None
        defender_strength = 0
        has_live_defenders = False
        if stronghold is not None:
            defending_armies = [
                army
                for army in located_armies
                if str(army.army_faction or "").strip() == str(stronghold.control or "").strip()
            ]
            defender_strength = sum(_live_warrior_count(army) for army in defending_armies)
            has_live_defenders = any(_army_has_live_detachments(army) for army in defending_armies)
        if stronghold and siege:
            besieger_faction = _active_siege_faction(session, siege)
            defender_commander_ids = {
                int(army.commander_id)
                for army in _defender_armies_in_stronghold(
                    session,
                    stronghold,
                    besieger_faction or "",
                )
                if army.commander_id is not None
            }
            siege_payload = {
                "under_siege": True,
                "stronghold_name": stronghold.stronghold_name,
                "besieger_faction": besieger_faction,
                "matin_ticks_elapsed": int(siege.matin_ticks_elapsed or 0),
            }
            if viewer_commander_id is not None and (
                viewer_commander_id in _active_siege_commander_ids(session, siege) or viewer_commander_id in defender_commander_ids
            ):
                siege_payload["gates_open"] = bool(siege.gates_open)
        cells.append(
            {
                "h3": location.location_id,
                "terrain_type": terrain_type,
                "has_road": location.is_road,
                "cell_title": _cell_title(
                    terrain_type=terrain_type,
                    has_road=bool(location.is_road),
                    stronghold_name=stronghold_name,
                    region_name=location.region,
                    other_armies=other_armies,
                ),
                "region": location.region,
                "settlement": location.settlement,
                "foraged_this_season": location.foraged_this_season,
                "stronghold": (
                    {
                        "id": _stronghold_ref(stronghold.stronghold_id),
                        "name": stronghold.stronghold_name,
                        "type": stronghold.stronghold_type,
                        "faction": stronghold.control,
                        "defender_strength": defender_strength,
                        "has_live_defenders": has_live_defenders,
                        "under_siege": bool(siege),
                        "siege": siege_payload,
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


def _status_signals_for_army(session: Session, army: Army, current_action: Action | None, standing: StandingOrder | None) -> list[dict[str, str]]:
    signals: list[dict[str, str]] = []
    stats = supply_stats(army)
    if int(army.army_supply or 0) <= 0 and int(stats.daily_consumption or 0) > 0:
        signals.append({"category": "supply", "importance": "high", "message": "The army has no supply."})
    if current_action is not None and current_action.kind == "rout" and current_action.state in ACTIVE_ACTION_STATES:
        signals.append({"category": "rout", "importance": "high", "message": "The army is routing."})
    if standing is not None and standing.forced_march_enabled:
        signals.append({"category": "standing-order", "importance": "moderate", "message": "Standing order: forced march."})
    enemies = session.query(Army).filter(
        Army.army_id != army.army_id,
        Army.army_faction != army.army_faction,
    ).all()
    nearby = [enemy for enemy in enemies if _grid_distance(army.location_id, enemy.location_id) <= _environs_radius_for_army(army)]
    if nearby:
        signals.append({"category": "enemy-proximity", "importance": "high", "message": "Enemy forces are nearby."})
    return signals


def _serialize_action(session: Session, action: Action, commander_id: int | None = None) -> dict[str, Any]:
    payload = {
        "action_id": _action_ref(action.action_id),
        "kind": action.kind,
        "state": action.state,
        "eta": None,
    }
    if action.eta_day is not None and action.eta_watch is not None:
        payload["eta"] = _to_watch_stamp(action.eta_day, action.eta_watch)
    try:
        params = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        params = {}
    if action.kind == "move":
        destination_h3 = params.get("destination_h3")
        if isinstance(destination_h3, str) and destination_h3.strip():
            payload["destination_h3"] = destination_h3.strip()
    if action.kind == "attack":
        target_h3 = params.get("target_h3")
        if isinstance(target_h3, str) and target_h3.strip():
            payload["target_h3"] = target_h3.strip()
    if action.kind == "besiege":
        target_h3 = params.get("target_h3")
        if isinstance(target_h3, str) and target_h3.strip():
            payload["target_h3"] = target_h3.strip()
        target_stronghold_id = params.get("target_stronghold_id")
        if target_stronghold_id is not None:
            payload["target_stronghold_id"] = _stronghold_ref(int(target_stronghold_id))
            stronghold = session.get(Stronghold, int(target_stronghold_id))
            if stronghold is not None:
                payload["target_stronghold_name"] = stronghold.stronghold_name
                siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
                if siege is not None and commander_id is not None:
                    besieger_faction = _active_siege_faction(session, siege) or ""
                    defender_ids = {
                        int(army.commander_id)
                        for army in _defender_armies_in_stronghold(session, stronghold, besieger_faction)
                        if army.commander_id is not None
                    }
                    if commander_id in _active_siege_commander_ids(session, siege) or commander_id in defender_ids:
                        payload["gates_open"] = bool(siege.gates_open)
    return payload


def _serialize_remaining_itinerary(session: Session, commander_id: int) -> dict[str, Any]:
    actions = (
        session.query(Action)
        .filter(
            Action.commander_id == commander_id,
            Action.state.in_(ACTIVE_ACTION_STATES),
        )
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )
    remaining_moves: list[str] = []
    remaining_rout: list[str] = []
    siege_target_h3: str | None = None
    for action in actions:
        try:
            params = json.loads(action.parameters_json or "{}")
        except json.JSONDecodeError:
            params = {}
        if action.kind == "move":
            destination_h3 = _get_destination_h3(action)
            if destination_h3:
                remaining_moves.append(destination_h3)
        elif action.kind == "rout" and action.state == "in_progress":
            path = [str(h3_index).strip() for h3_index in (params.get("path") or []) if str(h3_index).strip()]
            remaining_rout.extend(path)
        elif action.kind == "besiege" and action.state in ACTIVE_ACTION_STATES:
            target_h3 = str(params.get("target_h3") or "").strip()
            if target_h3:
                siege_target_h3 = target_h3
    return {
        "remaining_moves": remaining_moves,
        "remaining_rout": remaining_rout,
        "siege_target_h3": siege_target_h3,
    }


def _normalized_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _commander_name_exists(session: Session, name: str, *, exclude_commander_id: int | None = None) -> bool:
    normalized = _normalized_name(name)
    if not normalized:
        return False
    query = session.query(Commander).filter(Commander.commander_name.ilike(normalized))
    if exclude_commander_id is not None:
        query = query.filter(Commander.commander_id != exclude_commander_id)
    return query.first() is not None


def _army_name_exists(session: Session, name: str, *, exclude_army_ids: set[int] | None = None) -> bool:
    normalized = _normalized_name(name)
    if not normalized:
        return False
    query = session.query(Army).filter(Army.army_name.ilike(normalized))
    if exclude_army_ids:
        query = query.filter(~Army.army_id.in_(sorted(exclude_army_ids)))
    return query.first() is not None


def _management_supply_payload(current: int, *, detachments: list[Detachment], noncombatant_percent: float) -> dict[str, Any]:
    class _DraftArmy:
        pass

    draft = _DraftArmy()
    draft.detachments = detachments
    draft.noncombattant_percent = float(noncombatant_percent or 0.0)
    draft.army_supply = int(current or 0)
    draft.is_garrison = False
    stats = supply_stats(draft)
    return {
        "current": int(current or 0),
        "capacity": int(stats.capacity or 0),
        "daily_consumption": int(stats.daily_consumption or 0),
        "days_estimate": stats.days_estimate,
    }


def _management_supply_capacity(*, detachments: list[Detachment], noncombatant_percent: float) -> int:
    return int(
        _management_supply_payload(
            0,
            detachments=detachments,
            noncombatant_percent=noncombatant_percent,
        )["capacity"]
        or 0
    )


def _management_alert(session: Session, *, commander_id: int | None, army_name: str, clock: GameClock) -> None:
    if commander_id is None:
        return
    _create_alert(
        session,
        recipient_commander_id=commander_id,
        alert_type="action",
        signal_kind="event",
        category="army-management",
        importance="normal",
        message=f"Composition of {army_name} changed",
        created_day=clock.day,
        created_watch=clock.watch,
    )


def _copy_world_event_alerts(
    session: Session,
    *,
    source_commander_id: int,
    target_commander_id: int,
) -> None:
    source_alerts = (
        session.query(Alert)
        .filter(
            Alert.recipient_commander_id == source_commander_id,
            Alert.alert_type == "world event",
        )
        .order_by(Alert.alert_id.asc())
        .all()
    )
    for alert in source_alerts:
        try:
            payload = json.loads(alert.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        _create_alert(
            session,
            recipient_commander_id=target_commander_id,
            alert_type=alert.alert_type,
            signal_kind=alert.signal_kind,
            category=alert.category,
            importance=alert.importance,
            message=alert.message,
            payload=payload,
            created_day=alert.created_day,
            created_watch=alert.created_watch,
            delivered_day=alert.delivered_day,
            delivered_watch=alert.delivered_watch,
        )


def _validate_and_apply_management_transaction(
    session: Session,
    *,
    commander_id: int,
    clock: GameClock,
    payload: ArmyManagementApplyRequest,
) -> dict[str, Any]:
    left_army = (
        session.query(Army)
        .options(
            joinedload(Army.commander),
            joinedload(Army.detachments).joinedload(Detachment.specials),
        )
        .filter(Army.commander_id == commander_id)
        .first()
    )
    if left_army is None:
        raise _army_management_error("No army found for commander", status_code=404)
    submitted_left_army_id = _parse_army_ref(payload.left_army.army_id or "")
    if submitted_left_army_id != left_army.army_id:
        raise _army_management_error("Army management state is stale; reopen and try again.", status_code=409)
    if left_army.is_garrison:
        raise _army_management_error("Garrison armies cannot initiate army management.")
    if _army_is_routing(session, left_army):
        raise _army_management_error("Army is routing; new orders unavailable until regroup.", status_code=409)

    candidates = _eligible_management_armies(session, left_army)
    current_hash = _army_management_snapshot_hash(left_army, candidates)
    if str(payload.baseline_hash or "").strip() != current_hash:
        raise _army_management_error("Army management state is stale; reopen and try again.", status_code=409)

    existing_by_id = {army.army_id: army for army in candidates}
    right_mode = str(payload.right_target.mode or "").strip().lower()
    right_existing: Army | None = None
    right_army_payload = payload.right_army
    if right_mode == "existing":
        if right_army_payload is None:
            raise _army_management_error("Right army payload is required for existing-army management.")
        submitted_right_id = _parse_army_ref(payload.right_target.army_id or "")
        right_existing = existing_by_id.get(submitted_right_id)
        if right_existing is None:
            raise _army_management_error("Selected army is no longer eligible for management.", status_code=409)
        submitted_right_army_id = _parse_army_ref(right_army_payload.army_id or "")
        if submitted_right_army_id != submitted_right_id:
            raise _army_management_error("Army management state is stale; reopen and try again.", status_code=409)
        if right_existing.commander_id is not None and _army_is_routing(session, right_existing):
            raise _army_management_error("Army is routing; new orders unavailable until regroup.", status_code=409)
    elif right_mode == "none":
        if right_army_payload is not None:
            submitted_none_ids = {str(value).strip() for value in right_army_payload.detachment_ids if str(value).strip()}
            if submitted_none_ids:
                raise _army_management_error("Cannot assign detachments without selecting another army.")
    elif right_mode != "new":
        raise _army_management_error("right_target.mode must be 'existing', 'new', or 'none'.")

    original_left_det_ids = {int(det.detachment_id) for det in left_army.detachments}
    original_right_det_ids = {int(det.detachment_id) for det in right_existing.detachments} if right_existing is not None else set()
    source_det_ids = original_left_det_ids | original_right_det_ids

    left_det_ids = {_parse_detachment_ref(value) for value in payload.left_army.detachment_ids}
    right_det_ids = {_parse_detachment_ref(value) for value in (right_army_payload.detachment_ids if right_army_payload is not None else [])}
    if left_det_ids & right_det_ids:
        raise _army_management_error("Detachment lists may not overlap.")
    if left_det_ids | right_det_ids != source_det_ids:
        raise _army_management_error("Detachment assignment is stale or invalid.")
    if not left_det_ids:
        raise _army_management_error("Your army must retain at least one detachment.")
    if right_mode == "existing" and right_existing is not None and not right_existing.is_garrison and not right_det_ids:
        raise _army_management_error("Each army must have at least one detachment.")
    if right_mode == "new" and not right_det_ids:
        raise _army_management_error("A new army must have at least one detachment.")

    left_name = _normalized_name(payload.left_army.name)
    if not left_name:
        raise _army_management_error("Left army name is required.")
    right_name = _normalized_name(right_army_payload.name if right_army_payload is not None else "")
    if right_mode == "existing":
        if right_existing is not None and not right_existing.is_garrison and not right_name:
            raise _army_management_error("Right army name is required.")
    elif right_mode == "new":
        if not right_name:
            raise _army_management_error("New army name is required.")

    exclude_army_ids = {left_army.army_id}
    if right_existing is not None:
        exclude_army_ids.add(right_existing.army_id)
    seen_names: set[str] = set()
    for army_name in [left_name, right_name if right_mode == "new" or (right_existing is not None and not right_existing.is_garrison) else None]:
        if army_name is None:
            continue
        lowered = army_name.lower()
        if lowered in seen_names:
            raise _army_management_error("Army names must be unique.")
        seen_names.add(lowered)
        if _army_name_exists(session, army_name, exclude_army_ids=exclude_army_ids):
            raise _army_management_error("Army names must be globally unique.")

    original_left_supply = int(left_army.army_supply or 0)
    if right_mode == "existing" and right_existing is not None:
        if right_existing.is_garrison:
            left_supply = int(payload.left_army.supply_current or 0)
            right_supply = int(right_existing.army_supply or 0)
            if left_supply < 0:
                raise _army_management_error("Supply values may not be negative.")
            if right_army_payload is not None and right_army_payload.commander_id not in {None, ""}:
                raise _army_management_error("Cannot assign a commander to a garrison.")
            if right_army_payload is not None and right_army_payload.supply_current not in {None, int(right_existing.army_supply or 0), ""}:
                raise _army_management_error("Supply cannot be transferred to or from a garrison.")
        else:
            original_supply_sum = original_left_supply + int(right_existing.army_supply or 0)
            left_supply = int(payload.left_army.supply_current or 0)
            right_supply = int((right_army_payload.supply_current if right_army_payload is not None else 0) or 0)
            if left_supply < 0 or right_supply < 0:
                raise _army_management_error("Supply values may not be negative.")
            if left_supply + right_supply != original_supply_sum:
                raise _army_management_error("Supply totals must be conserved between the two armies.")
    elif right_mode == "new":
        left_supply = int(payload.left_army.supply_current or 0)
        right_supply = int((right_army_payload.supply_current if right_army_payload is not None else 0) or 0)
        if left_supply < 0 or right_supply < 0:
            raise _army_management_error("Supply values may not be negative.")
        if left_supply + right_supply != int(left_army.army_supply or 0):
            raise _army_management_error("Supply totals must be conserved when creating a new army.")
    else:
        left_supply = int(payload.left_army.supply_current or 0)
        right_supply = 0
        if left_supply != int(left_army.army_supply or 0):
            raise _army_management_error("Supply cannot be transferred without selecting another army.")

    right_commander_after_id: int | None = None
    left_commander_after_id: int | None = left_army.commander_id
    create_new_commander = right_mode == "new"
    if right_mode == "existing" and right_existing is not None:
        if right_existing.is_garrison:
            if right_army_payload is not None and right_army_payload.commander_id not in {None, ""}:
                raise _army_management_error("Cannot swap commanders with a garrison.")
        else:
            original_commander_ids = {
                int(value)
                for value in [left_army.commander_id, right_existing.commander_id]
                if value is not None
            }
            submitted_left_commander_id = _parse_commander_ref(payload.left_army.commander_id or "") if payload.left_army.commander_id else None
            submitted_right_commander_id = _parse_commander_ref(right_army_payload.commander_id or "") if right_army_payload is not None and right_army_payload.commander_id else None
            final_commander_ids = {
                int(value)
                for value in [submitted_left_commander_id, submitted_right_commander_id]
                if value is not None
            }
            if final_commander_ids != original_commander_ids:
                raise _army_management_error("Commander assignments are invalid.")
            if submitted_left_commander_id == right_existing.commander_id and submitted_right_commander_id == left_army.commander_id:
                if _commander_has_active_siege(session, left_army.commander_id) or _commander_has_active_siege(session, right_existing.commander_id):
                    raise _army_management_error("Cannot swap commanders while an involved army is maintaining a siege.")
            elif submitted_left_commander_id != left_army.commander_id or submitted_right_commander_id != right_existing.commander_id:
                raise _army_management_error("Commander assignments are invalid.")
            left_commander_after_id = submitted_left_commander_id
            right_commander_after_id = submitted_right_commander_id
    elif create_new_commander:
        new_commander = right_army_payload.new_commander if right_army_payload is not None else None
        if new_commander is None:
            raise _army_management_error("New army commander details are required.")
        commander_name = _normalized_name(new_commander.name)
        commander_title = _normalized_name(new_commander.title)
        if not commander_name or not commander_title:
            raise _army_management_error("New commander title and name are required.")
        if _commander_name_exists(session, commander_name):
            raise _army_management_error("Commander names must be globally unique.")

    detachment_rows = (
        session.query(Detachment)
        .options(joinedload(Detachment.specials))
        .filter(Detachment.detachment_id.in_(sorted(source_det_ids)))
        .all()
    )
    detachment_by_id = {det.detachment_id: det for det in detachment_rows}
    if set(detachment_by_id) != source_det_ids:
        raise _army_management_error("Detachment assignment is stale or invalid.")

    left_detachments_final = [detachment_by_id[det_id] for det_id in sorted(left_det_ids)]
    right_detachments_final = [detachment_by_id[det_id] for det_id in sorted(right_det_ids)]

    left_capacity_final = _management_supply_capacity(
        detachments=left_detachments_final,
        noncombatant_percent=float(left_army.noncombattant_percent or 0.0),
    )
    if right_mode == "existing" and right_existing is not None and right_existing.is_garrison:
        expected_left_supply = min(original_left_supply, left_capacity_final)
        if left_supply != expected_left_supply:
            raise _army_management_error(
                "Field army supply must remain unchanged except for unavoidable carrying-capacity loss."
            )
    if left_supply > left_capacity_final:
        raise _army_management_error("Left army supply exceeds its maximum carrying capacity.")

    if right_mode == "existing" and right_existing is not None and not right_existing.is_garrison:
        right_capacity_final = _management_supply_capacity(
            detachments=right_detachments_final,
            noncombatant_percent=float(right_existing.noncombattant_percent or 0.0),
        )
        if right_supply > right_capacity_final:
            raise _army_management_error("Right army supply exceeds its maximum carrying capacity.")
    elif right_mode == "new":
        right_capacity_final = _management_supply_capacity(
            detachments=right_detachments_final,
            noncombatant_percent=float(left_army.noncombattant_percent or 0.0),
        )
        if right_supply > right_capacity_final:
            raise _army_management_error("New army supply exceeds its maximum carrying capacity.")

    affected_existing_field_armies = [left_army]
    if right_existing is not None and not right_existing.is_garrison:
        affected_existing_field_armies.append(right_existing)

    cancelled_actions: dict[str, Any] = {}
    for army in affected_existing_field_armies:
        cancelled_actions[_army_ref(army.army_id)] = _cancel_non_siege_actions_for_commander(session, army.commander_id)

    created_commander: Commander | None = None
    created_army: Army | None = None
    if create_new_commander:
        new_commander = right_army_payload.new_commander if right_army_payload is not None else None
        assert new_commander is not None
        created_commander = Commander(
            commander_name=_normalized_name(new_commander.name),
            commander_title=_normalized_name(new_commander.title),
            commander_age=30,
            created_by_commander_id=commander_id,
            created_day=clock.day,
            created_watch=clock.watch,
        )
        session.add(created_commander)
        session.flush()
        _copy_world_event_alerts(
            session,
            source_commander_id=commander_id,
            target_commander_id=created_commander.commander_id,
        )
        created_army = Army(
            location_id=left_army.location_id,
            army_name=right_name,
            army_faction=left_army.army_faction,
            commander_id=created_commander.commander_id,
            garrison_stronghold_id=None,
            army_supply=right_supply,
            army_morale=int(left_army.army_morale or 9),
            army_resting_morale=int(left_army.army_resting_morale or left_army.army_morale or 9),
            is_embarked=False,
            is_garrison=False,
            noncombattant_percent=float(left_army.noncombattant_percent or 0.0),
        )
        session.add(created_army)
        session.flush()
        right_existing = created_army
        right_commander_after_id = created_commander.commander_id
    if right_mode != "none":
        assert right_existing is not None

    left_army.army_name = left_name
    if right_existing is not None and not right_existing.is_garrison:
        right_existing.army_name = right_name

    if right_existing is not None and not right_existing.is_garrison:
        left_army.army_supply = left_supply
        right_existing.army_supply = right_supply
    else:
        left_army.army_supply = left_supply

    if create_new_commander:
        left_army.commander_id = left_commander_after_id
    elif right_existing is not None and not right_existing.is_garrison:
        left_army.commander_id = left_commander_after_id
        right_existing.commander_id = right_commander_after_id

    for det_id in left_det_ids:
        det = detachment_by_id[det_id]
        det.army_id = left_army.army_id
        det.army = left_army
    if right_existing is not None:
        for det_id in right_det_ids:
            det = detachment_by_id[det_id]
            det.army_id = right_existing.army_id
            det.army = right_existing

    if right_existing is not None and not right_existing.is_garrison:
        _clamp_army_supply_to_capacity(left_army)
        _clamp_army_supply_to_capacity(right_existing)
    else:
        _clamp_army_supply_to_capacity(left_army)

    alert_pairs: list[tuple[int | None, str]] = []
    refreshed_left_commander_id = left_army.commander_id
    alert_pairs.append((refreshed_left_commander_id, left_army.army_name))
    if right_existing is not None and right_existing.commander_id is not None:
        alert_pairs.append((right_existing.commander_id, right_existing.army_name))
    seen_alert_targets: set[tuple[int | None, str]] = set()
    for alert_commander_id, alert_army_name in alert_pairs:
        key = (alert_commander_id, alert_army_name)
        if key in seen_alert_targets:
            continue
        seen_alert_targets.add(key)
        _management_alert(session, commander_id=alert_commander_id, army_name=alert_army_name, clock=clock)

    session.flush()
    if created_army is not None:
        record_history_event(
            session,
            event_key=f"army:{created_army.army_id}:created",
            world_tick=clock.world_tick,
            event_kind="army_created",
            location_id=created_army.location_id,
            payload={"army": army_history_payload(created_army), "location_h3": created_army.location_id},
        )
    active_commander_army = _find_commander_army(session, commander_id)
    return {
        "result": "ok",
        "left_army_id": _army_ref(left_army.army_id),
        "right_army_id": _army_ref(right_existing.army_id) if right_existing is not None else None,
        "created_army_id": _army_ref(created_army.army_id) if created_army is not None else None,
        "created_commander_id": _commander_ref(created_commander.commander_id) if created_commander is not None else None,
        "active_commander_army_id": _army_ref(active_commander_army.army_id),
        "cancelled_actions": cancelled_actions,
        "alerts_created_for": [
            _commander_ref(commander_value)
            for commander_value, _ in seen_alert_targets
            if commander_value is not None
        ],
    }


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


def _claimable_commanders(session: Session) -> list[Commander]:
    return (
        session.query(Commander)
        .join(Army, Army.commander_id == Commander.commander_id)
        .filter(Army.is_garrison.is_(False))
        .order_by(Commander.commander_id.asc())
        .all()
    )


def _validate_admin_token(x_admin_token: str | None) -> None:
    configured_admin_token = os.getenv("ADMIN_TOKEN") or os.getenv("DEV_ADMIN_TOKEN")
    if not configured_admin_token:
        raise HTTPException(status_code=503, detail="Administrative access is not configured")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, configured_admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _issue_session(session: Session, commander_id: int, client_kind: str, raw_token: str | None = None) -> tuple[str, AuthToken]:
    raw_token = raw_token or secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    auth_token = AuthToken(
        token=_token_hash(raw_token),
        commander_id=commander_id,
        created_at=now,
        last_used_at=now,
        revoked_at=None,
        client_kind=client_kind,
    )
    session.add(auth_token)
    return raw_token, auth_token


def _validate_game_password(candidate: str) -> None:
    configured = os.getenv("GAME_PASSWORD")
    if not configured:
        raise HTTPException(status_code=503, detail="Player claiming is not configured")
    if not secrets.compare_digest(candidate, configured):
        raise HTTPException(status_code=401, detail="Invalid game password")


def _claim_token(commander_id: int, client_kind: str, idempotency_key: str) -> str:
    secret = os.getenv("SESSION_SECRET") or os.getenv("ADMIN_TOKEN") or os.getenv("GAME_PASSWORD")
    if not secret:
        raise HTTPException(status_code=503, detail="Session signing is not configured")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"claim:{commander_id}:{client_kind}:{idempotency_key}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@router.get("/auth/commanders")
def auth_commanders(session: Session = Depends(_get_session)):
    return list_commander_claims(session=session)


@router.post("/auth/claim")
def claim_session(
    payload: ClaimRequest,
    response: Response,
    session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    _validate_game_password(payload.game_password)
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    def operation():
        commander_pk = _parse_commander_ref(payload.commander_id)
        commander = session.get(Commander, commander_pk)
        if commander is None:
            raise HTTPException(status_code=404, detail="Commander not found")
        claimable_ids = {row.commander_id for row in _claimable_commanders(session)}
        if commander_pk not in claimable_ids:
            raise HTTPException(status_code=400, detail="Commander is not available for player claiming")
        request_data = {"commander_id": payload.commander_id, "client_kind": payload.client_kind, "game_password": payload.game_password}
        request_hash = hashlib.sha256(json.dumps(request_data, sort_keys=True).encode("utf-8")).hexdigest()
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"), {"lock_name": f"claim:{commander_pk}:{idempotency_key}"})
        existing_record = session.query(IdempotencyRecord).filter(
            IdempotencyRecord.actor_scope == f"claim:{commander_pk}",
            IdempotencyRecord.route == "claim",
            IdempotencyRecord.idempotency_key == idempotency_key,
        ).one_or_none()
        raw_token = _claim_token(commander_pk, payload.client_kind, idempotency_key)
        if existing_record is not None:
            if existing_record.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with a different request")
            claim = session.get(CommanderClaim, commander_pk)
            auth_token = session.get(AuthToken, _token_hash(raw_token))
            if claim is None or auth_token is None or auth_token.revoked_at is not None:
                raise HTTPException(status_code=409, detail="The original claim is no longer active")
            result = {"commander": _serialize_claim_commander(session, commander, claim), "client_kind": payload.client_kind}
            if payload.client_kind == "api":
                result["token"] = raw_token
            else:
                response.set_cookie(SESSION_COOKIE_NAME, raw_token, max_age=60 * 60 * 24 * 365, httponly=True,
                                    secure=os.getenv("APP_ENV", "development").lower() == "production", samesite="strict", path="/")
            return result
        if session.get(CommanderClaim, commander_pk) is not None:
            raise HTTPException(status_code=409, detail="Commander has already been claimed")
        raw_token, auth_token = _issue_session(session, commander_pk, payload.client_kind, raw_token=raw_token)
        claim = CommanderClaim(
            commander_id=commander_pk,
            token=auth_token.token,
            claimed_at=datetime.now(timezone.utc),
        )
        session.add(claim)
        session.add(IdempotencyRecord(
            actor_scope=f"claim:{commander_pk}", route="claim", idempotency_key=idempotency_key,
            request_hash=request_hash, response_json="{}", created_at=datetime.now(timezone.utc)
        ))
        result = {"commander": _serialize_claim_commander(session, commander, claim), "client_kind": payload.client_kind}
        if payload.client_kind == "api":
            result["token"] = raw_token
        else:
            response.set_cookie(
                SESSION_COOKIE_NAME,
                raw_token,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                secure=os.getenv("APP_ENV", "development").lower() == "production",
                samesite="strict",
                path="/",
            )
        return result

    return _run_world_mutation(session, operation)


@router.get("/auth/session")
def current_session(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    commander = session.get(Commander, commander_id)
    if commander is None:
        raise HTTPException(status_code=401, detail="Invalid session")
    return {"commander": _serialize_claim_commander(session, commander, session.get(CommanderClaim, commander_id))}


@router.post("/auth/logout")
def logout_session(
    response: Response,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    def operation():
        claim = session.get(CommanderClaim, commander_id)
        if claim is not None:
            auth_token = session.get(AuthToken, claim.token)
            if auth_token is not None:
                auth_token.revoked_at = datetime.now(timezone.utc)
            session.delete(claim)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"released": True}

    return _run_world_mutation(session, operation)


def login(
    payload: LoginRequest,
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
):
    configured_admin_token = os.getenv("ADMIN_TOKEN") or os.getenv("DEV_ADMIN_TOKEN")
    is_admin_login = bool(configured_admin_token and x_admin_token == configured_admin_token)

    def operation():
        commander = (
            session.query(Commander)
            .filter(Commander.commander_name.ilike(payload.commander_name.strip()))
            .first()
        )
        if commander is None:
            raise HTTPException(status_code=404, detail="Commander not found")

        token, auth_token = _issue_session(session, commander.commander_id, "api")
        now = datetime.now(timezone.utc)
        if not is_admin_login:
            claimable_ids = {row.commander_id for row in _claimable_commanders(session)}
            if commander.commander_id not in claimable_ids:
                raise HTTPException(status_code=400, detail="Commander is not available for player claiming")
            if session.get(CommanderClaim, commander.commander_id) is not None:
                raise HTTPException(status_code=409, detail="Commander has already been claimed")
            session.add(CommanderClaim(commander_id=commander.commander_id, token=auth_token.token, claimed_at=now))
        return {
            "token": token,
            "commander": {
                "id": _commander_ref(commander.commander_id),
                "name": commander.commander_name,
            },
        }

    return _run_world_mutation(session, operation)


@router.get("/commander-portraits/{filename}", include_in_schema=False)
def commander_portrait(filename: str):
    safe_names = {
        path.name
        for pattern in ("Portrait - *", "portrait_*.png")
        for path in COMMANDER_PORTRAIT_DIR.glob(pattern)
        if path.is_file()
    }
    if filename not in safe_names:
        raise HTTPException(status_code=404, detail="Portrait not found")
    return FileResponse(COMMANDER_PORTRAIT_DIR / filename)


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


def list_commander_claims(session: Session = Depends(_get_session)):
    claims = {claim.commander_id: claim for claim in session.query(CommanderClaim).all()}
    return [
        _serialize_claim_commander(session, commander, claims.get(commander.commander_id))
        for commander in _claimable_commanders(session)
    ]


def claim_commander(commander_id: str, session: Session = Depends(_get_session)):
    def operation():
        commander_pk = _parse_commander_ref(commander_id)
        commander = session.get(Commander, commander_pk)
        if commander is None:
            raise HTTPException(status_code=404, detail="Commander not found")
        claimable_ids = {row.commander_id for row in _claimable_commanders(session)}
        if commander.commander_id not in claimable_ids:
            raise HTTPException(status_code=400, detail="Commander is not available for player claiming")
        if session.get(CommanderClaim, commander.commander_id) is not None:
            raise HTTPException(status_code=409, detail="Commander has already been claimed")

        token, auth_token = _issue_session(session, commander.commander_id, "api")
        now = datetime.now(timezone.utc)
        claim = CommanderClaim(commander_id=commander.commander_id, token=auth_token.token, claimed_at=now)
        session.add(claim)
        return {
            "token": token,
            "commander": _serialize_claim_commander(session, commander, claim),
        }

    return _run_world_mutation(session, operation)


@router.post("/admin/claims/reset")
def reset_commander_claims(
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
):
    _validate_admin_token(x_admin_token)

    def operation():
        claims = session.query(CommanderClaim).all()
        tokens = [claim.token for claim in claims]
        for claim in claims:
            session.delete(claim)
        if tokens:
            session.flush()
            session.query(AuthToken).filter(AuthToken.token.in_(tokens)).delete(synchronize_session=False)
        return {"reset_claims": len(claims)}

    return _run_world_mutation(session, operation)


@router.post("/admin/claims/{commander_id}/release")
def release_commander_claim(
    commander_id: str,
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
):
    _validate_admin_token(x_admin_token)

    def operation():
        commander_pk = _parse_commander_ref(commander_id)
        claim = session.get(CommanderClaim, commander_pk)
        if claim is None:
            raise HTTPException(status_code=404, detail="Commander claim not found")
        auth_token = session.get(AuthToken, claim.token)
        if auth_token is not None:
            auth_token.revoked_at = datetime.now(timezone.utc)
        session.delete(claim)
        return {"released_commander_id": _commander_ref(commander_pk)}

    return _run_world_mutation(session, operation)


@router.get("/admin/armies/summary")
def admin_armies_summary(
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
):
    _validate_admin_token(x_admin_token)
    armies = (
        session.query(Army)
        .options(joinedload(Army.commander), joinedload(Army.detachments))
        .filter(Army.commander_id.is_not(None), Army.is_garrison.is_(False))
        .order_by(Army.army_id.asc())
        .all()
    )
    rows = []
    claims = {claim.commander_id for claim in session.query(CommanderClaim.commander_id).all()}
    for army in armies:
        current_action = _get_current_action_row(session, int(army.commander_id))
        if current_action is None:
            status = "idle"
        elif current_action.state == "in_progress":
            status = "routing" if current_action.kind == "rout" else f"{current_action.kind} in progress"
        else:
            status = f"{current_action.kind} queued"
        rows.append(
            {
                "army_id": _army_ref(army.army_id),
                "army_name": army.army_name,
                "commander_id": _commander_ref(int(army.commander_id)),
                "commander_name": _commander_display_name(army.commander) if army.commander else "",
                "faction": army.army_faction,
                "claimed": int(army.commander_id) in claims,
                "location": describe_army_location(session, army.location_id),
                "status": status,
            }
        )
    return rows


@router.get("/admin/commanders/{commander_id}/brief", response_class=PlainTextResponse)
def admin_commander_brief(
    commander_id: str,
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
):
    _validate_admin_token(x_admin_token)
    return _commander_brief_text(session, _parse_commander_ref(commander_id))


@router.get("/time")
def get_time(session: Session = Depends(_get_session)):
    return _clock_payload(_get_or_create_clock(session))


@router.post("/admin/time/advance")
def advance_time_for_development(
    payload: TimeAdvanceRequest,
    session: Session = Depends(_get_session),
    x_admin_token: str | None = Header(default=None),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    if payload.steps < 1:
        raise HTTPException(status_code=400, detail="steps must be >= 1")

    _validate_admin_token(x_admin_token)

    def operation():
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(1180298062)"))
        clock = _get_or_create_clock(session, for_update="exclusive")
        start = _clock_payload(clock)
        timeline = []
        actions_started = 0
        actions_completed = 0
        actions_failed = 0

        for _ in range(payload.steps):
            capture_world_snapshot(session, clock, is_final=True)
            # Compare the actual state immediately before and after this watch's
            # processing. Reconstructing the state at the start of the departing
            # watch made a transition look new again on the following watch.
            siege_state_before_tick = _active_sieged_stronghold_ids(session)
            clock.world_tick += 1
            clock.day, watch_enum = from_world_tick(clock.world_tick)
            clock.watch = int(watch_enum)
            _auto_disable_forced_march_at_night(session, clock)
            message_result = _process_messages_tick(session, clock)
            tick_result = {"started": 0, "completed": 0, "failed": 0}
            if payload.execute_actions:
                _process_sieges_matin_tick(session, clock)
                tick_result = _execute_action_tick(session, clock)
                _auto_apply_follow_road_orders(session, clock)
                actions_started += tick_result["started"]
                actions_completed += tick_result["completed"]
                actions_failed += tick_result["failed"]
            _emit_siege_transition_alerts(
                session,
                start_stronghold_ids=siege_state_before_tick,
                clock=clock,
            )
            supply_result = None
            if clock.watch == int(Watch.NIGHT):
                # Night supply checks run after action resolution so completed forage can replenish first.
                supply_result = consume_supply_for_all_armies(session)
                _emit_supply_alerts_after_consumption(session, clock)
            # Transient conditions are derived in /me/view.status_signals rather
            # than appended to the durable event stream every watch.
            capture_world_snapshot(session, clock, is_final=False)
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

    return _run_idempotent_mutation(
        session,
        actor_scope="admin",
        route="advance-time",
        idempotency_key=idempotency_key,
        payload=payload,
        operation=operation,
    )


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
        .order_by(
            Message.delivery_day.desc(),
            _watch_chronological_order_sql(Message.delivery_watch).desc(),
            Message.message_id.desc(),
        )
        .all()
    )

    current_action = _get_current_action_row(session, commander_id)
    standing_order = session.get(StandingOrder, commander_id)

    return {
        "time": _clock_payload(clock),
        "army": _serialize_army(army),
        "environs": _serialize_environs(
            session,
            army.location_id,
            environs_radius,
            exclude_army_id=army.army_id,
            viewer_commander_id=commander_id,
            viewer_army=army,
        ),
        "messages": _serialize_message_summary(delivered_messages),
        "current_action": _serialize_action(session, current_action, commander_id) if current_action else None,
        "itinerary": _serialize_remaining_itinerary(session, commander_id),
        "standing_orders": _serialize_standing_orders(standing_order),
        "status_signals": _status_signals_for_army(session, army, current_action, standing_order),
    }


def _diegetic_action_position(session: Session, location_h3: str) -> str:
    location = str(location_h3 or "").strip()
    if not location:
        return ""
    stronghold = _stronghold_at_h3(session, location)
    if stronghold is not None:
        return str(stronghold.stronghold_name or "the target stronghold").strip()
    description = describe_army_location(session, location)
    if description == "at an unknown location":
        return "an undescribed destination"
    return f"a position {description}"


def _brief_action_target(session: Session, action: Action | None, army: Army) -> str:
    if action is None:
        return ""
    try:
        params = json.loads(action.parameters_json or "{}")
    except json.JSONDecodeError:
        params = {}
    if action.kind == "besiege":
        name = str(params.get("target_stronghold_name") or "").strip()
        if not name and params.get("target_stronghold_id") is not None:
            stronghold = session.get(Stronghold, int(params["target_stronghold_id"]))
            name = str(stronghold.stronghold_name or "").strip() if stronghold else ""
        return f"against {name or 'the target stronghold'}"
    if action.kind == "attack":
        target_army = None
        if params.get("target_army_id") is not None:
            target_army = session.get(Army, int(params["target_army_id"]))
        if target_army is not None:
            name = str(target_army.army_name or "").strip()
            if name:
                return f"against {name}"
        position = _diegetic_action_position(session, str(params.get("target_h3") or ""))
        return f"against forces at {position}" if position else "against the enemy"
    if action.kind == "move":
        return describe_march_stage(
            session,
            army.location_id,
            str(params.get("destination_h3") or ""),
        )
    if action.kind == "rout":
        path = [str(value).strip() for value in params.get("path", []) if str(value).strip()]
        position = _diegetic_action_position(session, path[0] if path else "")
        return f"toward {position}" if position else ""
    return ""


def _brief_siege_target(session: Session, army: Army) -> str:
    siege = _active_siege_for_besieger(session, army.army_id)
    if siege is None:
        return ""
    stronghold = session.get(Stronghold, siege.stronghold_id)
    if stronghold is None:
        return "the target stronghold"
    return str(stronghold.stronghold_name or "the target stronghold").strip()


def _brief_route_endpoint(session: Session, itinerary: dict[str, Any]) -> str:
    remaining_moves = itinerary.get("remaining_moves", [])
    if not isinstance(remaining_moves, list) or len(remaining_moves) <= 1:
        return ""
    return describe_route_endpoint(session, str(remaining_moves[-1] or ""))


def _brief_action_eta(action: Action | None) -> str:
    if action is None or action.eta_day is None or action.eta_watch is None:
        return ""
    event_date = _scenario_date_for_day(int(action.eta_day))
    watch_name = WATCH_LABELS.get(Watch(int(action.eta_watch)), "unknown")
    return (
        f"during the {watch_name} watch on "
        f"{event_date.strftime('%B')} {event_date.day}, {event_date.year}"
    )


def _brief_unread_alert_counts(
    session: Session,
    *,
    commander_id: int,
    world_tick: int,
) -> tuple[int, int]:
    rows = (
        session.query(Alert.importance)
        .join(AlertRecipient, AlertRecipient.alert_id == Alert.alert_id)
        .filter(
            AlertRecipient.commander_id == commander_id,
            AlertRecipient.available_tick <= world_tick,
            AlertRecipient.read_at.is_(None),
            Alert.signal_kind == "event",
        )
        .all()
    )
    return len(rows), sum(1 for row in rows if str(row[0] or "").strip().lower() == "high")


def _brief_unread_letter_count(
    session: Session,
    *,
    commander_id: int,
    clock: GameClock,
) -> int:
    return (
        session.query(Message.message_id)
        .filter(
            Message.recipient_id == commander_id,
            Message.status == "received",
            Message.is_read.is_(False),
            _is_delivered_filter(clock.day, clock.watch),
        )
        .count()
    )


def _commander_brief_text(session: Session, commander_id: int) -> str:
    clock = _get_or_create_clock(session)
    army = _find_commander_army(session, commander_id)
    environs_radius = _environs_radius_for_army(army)
    environs = _serialize_environs(
        session,
        army.location_id,
        environs_radius,
        exclude_army_id=army.army_id,
        viewer_commander_id=commander_id,
        viewer_army=army,
    )
    visible_set = {str(cell.get("h3")) for cell in environs["cells"]}
    current_action = _get_current_action_row(session, commander_id)
    current_action_payload = (
        _serialize_action(session, current_action, commander_id)
        if current_action
        else None
    )
    itinerary = _serialize_remaining_itinerary(session, commander_id)
    standing_row = session.get(StandingOrder, commander_id)
    standing = _serialize_standing_orders(standing_row)
    status_signals = _status_signals_for_army(session, army, current_action, standing_row)
    unread_alerts, high_alerts = _brief_unread_alert_counts(
        session,
        commander_id=commander_id,
        world_tick=int(clock.world_tick),
    )
    sections = build_commander_brief(
        army=_serialize_army(army),
        time=_clock_payload(clock),
        environs=environs,
        current_action=current_action_payload,
        itinerary=itinerary,
        standing_orders=standing,
        action_target=_brief_action_target(session, current_action, army),
        action_eta=_brief_action_eta(current_action),
        route_endpoint=_brief_route_endpoint(session, itinerary),
        siege_target=_brief_siege_target(session, army),
        unread_letters=_brief_unread_letter_count(
            session,
            commander_id=commander_id,
            clock=clock,
        ),
        unread_alerts=unread_alerts,
        high_importance_alerts=high_alerts,
        status_signals=status_signals,
        border_road_cells=_border_road_neighbor_ids(session, visible_set),
    )
    return sections.render()


@router.get("/me/brief", response_class=PlainTextResponse)
def get_my_brief(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    return _commander_brief_text(session, commander_id)


@router.get("/me/map/strongholds")
def get_historical_stronghold_map(
    _commander_id: int = Depends(_get_current_commander_id),
):
    try:
        return load_historical_stronghold_catalog()
    except ScenarioCatalogError as exc:
        raise HTTPException(
            status_code=503,
            detail="Historical stronghold map annotations are unavailable.",
        ) from exc


@router.get("/me/navigation/route")
def get_navigation_route_summary(
    destination: str = Query(..., min_length=1, max_length=32),
    origin: str = Query(default="current", min_length=1, max_length=32),
    allow_off_road: bool = Query(default=False),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    """Describe a static-world route without returning its underlying H3 cells."""
    army = _find_commander_army(session, commander_id)
    destination_id = _parse_stronghold_ref(destination.strip())
    destination_stronghold = session.get(Stronghold, destination_id)
    if destination_stronghold is None:
        raise HTTPException(status_code=404, detail="Destination stronghold not found")

    origin_value = origin.strip()
    origin_stronghold = None
    if origin_value.casefold() != "current":
        origin_id = _parse_stronghold_ref(origin_value)
        origin_stronghold = session.get(Stronghold, origin_id)
        if origin_stronghold is None:
            raise HTTPException(status_code=404, detail="Origin stronghold not found")

    try:
        return build_route_summary(
            session,
            army=army,
            origin=origin_stronghold,
            destination=destination_stronghold,
            allow_off_road=allow_off_road,
        )
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/me/army-management")
def get_army_management_state(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    clock = _get_or_create_clock(session)
    left_army = (
        session.query(Army)
        .options(
            joinedload(Army.commander),
            joinedload(Army.detachments).joinedload(Detachment.specials),
        )
        .filter(Army.commander_id == commander_id)
        .first()
    )
    if left_army is None:
        raise HTTPException(status_code=404, detail="No army found for commander")
    eligible = _eligible_management_armies(session, left_army)
    baseline_hash = _army_management_snapshot_hash(left_army, eligible)
    return {
        "time": {
            "day": clock.day,
            "watch": clock.watch,
            "watch_name": WATCH_LABELS.get(Watch(int(clock.watch)), "unknown").capitalize(),
        },
        "active_commander": _serialize_management_commander(left_army.commander),
        "left_army": _serialize_management_army(left_army),
        "other_armies": [_serialize_management_army(army) for army in eligible],
        "new_army_template": _army_management_new_army_template(session, left_army.army_faction),
        "baseline": {
            "left_army_id": _army_ref(left_army.army_id),
            "location_h3": left_army.location_id,
            "faction": left_army.army_faction,
            "pair_options": [_army_ref(army.army_id) for army in eligible] + ["NEW_ARMY"],
            "snapshot_hash": baseline_hash,
        },
    }


@router.get("/me/army-management/new-army-template")
def get_army_management_new_army_template(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    left_army = _find_commander_army(session, commander_id)
    if left_army is None:
        raise HTTPException(status_code=404, detail="No army found for commander")
    if left_army.is_garrison:
        raise HTTPException(status_code=400, detail="Garrison armies cannot initiate army management.")
    return _army_management_new_army_template(session, left_army.army_faction)


@router.post("/me/army-management/apply")
def apply_army_management(
    payload: ArmyManagementApplyRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        clock = _get_or_create_clock(session, for_update=True)
        return _validate_and_apply_management_transaction(
            session,
            commander_id=commander_id,
            clock=clock,
            payload=payload,
        )

    return _run_idempotent_mutation(
        session,
        actor_scope=f"commander:{commander_id}",
        route="army-management",
        idempotency_key=idempotency_key,
        payload=payload,
        operation=operation,
    )


def _border_road_neighbor_ids(session: Session, visible_set: set[str]) -> list[str]:
    neighbor_candidates: set[str] = set()
    for cell in visible_set:
        try:
            neighbors = set(h3.grid_ring(cell, 1))
        except Exception:
            continue
        neighbor_candidates.update(neighbors - visible_set)

    if not neighbor_candidates:
        return []

    road_neighbors = (
        session.query(Location.location_id)
        .filter(Location.location_id.in_(neighbor_candidates), Location.is_road.is_(True))
        .order_by(Location.location_id.asc())
        .all()
    )
    return [str(row[0]) for row in road_neighbors]


@router.get("/me/roads/border")
def get_border_road_neighbors(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    visible_set = set(h3.grid_disk(army.location_id, _environs_radius_for_army(army)))
    return {"roads": _border_road_neighbor_ids(session, visible_set)}


def _validated_staged_origin(session: Session, army: Army, staged_path: str | None) -> tuple[str, list[str]]:
    path = [value.strip() for value in str(staged_path or "").split(",") if value.strip()]
    if len(path) > 25:
        raise HTTPException(status_code=400, detail="Staged path is too long")
    origin = army.location_id
    for destination in path:
        valid = list_valid_destinations_from_origin(session, army.army_id, origin)
        if destination not in valid or _is_enemy_occupied(session, destination_h3=destination, moving_army=army):
            raise HTTPException(status_code=400, detail="Staged path is not reachable from the army's current location")
        origin = destination
    clock = _get_or_create_clock(session)
    total_cost = _path_watches_for_army(session, army, army.location_id, path)
    budget = _remaining_march_watch_budget_for_watch(
        int(clock.watch), army, _forced_march_enabled_for_army(session, army)
    )
    if total_cost > budget:
        raise HTTPException(status_code=400, detail="Staged path exceeds the army's remaining watch budget")
    return origin, path


@router.get("/me/actions/valid-next")
def get_valid_next_destinations(
    staged_path: str | None = Query(default=None, description="Comma-separated staged path from the current army location"),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    origin, path = _validated_staged_origin(session, army, staged_path)
    if not origin:
        raise HTTPException(status_code=400, detail="No origin location available")

    if session.get(Location, origin) is None:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Unknown origin_h3",
                "origin_h3": origin,
            },
        )

    try:
        valid = list_valid_destinations_from_origin(session, army.army_id, origin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    clock = _get_or_create_clock(session)
    budget = _remaining_march_watch_budget_for_watch(
        int(clock.watch), army, _forced_march_enabled_for_army(session, army)
    )
    filtered = [
        h3_index for h3_index in valid
        if not _is_enemy_occupied(session, destination_h3=h3_index, moving_army=army)
        and _path_watches_for_army(session, army, army.location_id, path + [h3_index]) <= budget
    ]
    destinations: list[dict[str, Any]] = []
    for destination_h3 in sorted(filtered):
        watches_needed = calculate_move_watches_from_origin(session, army.army_id, origin, destination_h3)
        destinations.append(
            {
                "h3": destination_h3,
                "watches_needed": watches_needed,
            }
        )
    return {"origin_h3": origin, "valid_destinations": sorted(filtered), "destinations": destinations}


@router.get("/me/actions/valid-attack")
def get_valid_attack_targets(
    staged_path: str | None = Query(default=None, description="Comma-separated staged path from the current army location"),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    if _army_is_in_stronghold(session, army):
        _, _, valid_targets = _valid_stronghold_attack_targets(session, army)
        return {
            "origin_h3": army.location_id,
            "targets": [
                {
                    "target_h3": target.location_id,
                    "target_army_id": _army_ref(target.army_id),
                    "faction": target.army_faction,
                    "label": str(target.army_name or f"{target.army_faction} army").strip(),
                }
                for target in valid_targets
            ],
        }
    active_siege = _active_siege_for_besieger(session, army.army_id)
    if active_siege is not None:
        stronghold = session.get(Stronghold, active_siege.stronghold_id)
        if stronghold is not None:
            defenders = _defender_armies_in_stronghold(session, stronghold, army.army_faction)
            targets = []
            for defender in defenders:
                targets.append(
                    {
                        "target_h3": stronghold.location_id,
                        "target_army_id": _army_ref(defender.army_id),
                        "faction": defender.army_faction,
                        "label": str(defender.army_name or f"{defender.army_faction} army").strip(),
                    }
                )
            return {"origin_h3": army.location_id, "targets": targets}
    origin, _ = _validated_staged_origin(session, army, staged_path)
    if not origin:
        raise HTTPException(status_code=400, detail="No origin location available")
    if session.get(Location, origin) is None:
        raise HTTPException(
            status_code=400,
            detail={"message": "Unknown origin_h3", "origin_h3": origin},
        )
    try:
        neighbors = set(h3.grid_ring(origin, 1))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to determine adjacent cells: {exc}") from exc
    enemies = (
        session.query(Army)
        .join(Detachment, Detachment.army_id == Army.army_id)
        .filter(
            Army.location_id.in_(list(neighbors)),
            Army.army_id != army.army_id,
            Army.army_faction != army.army_faction,
            Detachment.warrior_count > 0,
        )
        .distinct()
        .order_by(Army.army_id.asc())
        .all()
    )
    targets = []
    for enemy in enemies:
        enemy_stronghold = _stronghold_at_h3(session, enemy.location_id)
        if enemy_stronghold is not None:
            continue
        label = str(enemy.army_name or f"{enemy.army_faction} army").strip()
        targets.append(
            {
                "target_h3": enemy.location_id,
                "target_army_id": _army_ref(enemy.army_id),
                "faction": enemy.army_faction,
                "label": label,
            }
        )
    return {"origin_h3": origin, "targets": targets}


@router.get("/me/actions/valid-besiege")
def get_valid_besiege_targets(
    staged_path: str | None = Query(default=None, description="Comma-separated staged path from the current army location"),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    if _army_is_in_stronghold(session, army):
        return {"origin_h3": army.location_id, "targets": []}
    origin, _ = _validated_staged_origin(session, army, staged_path)
    if not origin:
        raise HTTPException(status_code=400, detail="No origin location available")
    if session.get(Location, origin) is None:
        raise HTTPException(status_code=400, detail={"message": "Unknown origin_h3", "origin_h3": origin})
    try:
        neighbors = set(h3.grid_ring(origin, 1))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to determine adjacent cells: {exc}") from exc
    strongholds = (
        session.query(Stronghold)
        .filter(Stronghold.location_id.in_(list(neighbors)), Stronghold.control != army.army_faction)
        .order_by(Stronghold.stronghold_id.asc())
        .all()
    )
    targets = []
    for stronghold in strongholds:
        defenders = _defender_armies_in_stronghold(session, stronghold, army.army_faction)
        if not defenders:
            continue
        active_siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
        if active_siege is not None:
            besieger_faction = _active_siege_faction(session, active_siege)
            if besieger_faction and besieger_faction != army.army_faction:
                continue
        targets.append(
            {
                "stronghold_id": _stronghold_ref(stronghold.stronghold_id),
                "stronghold_name": stronghold.stronghold_name,
                "target_h3": stronghold.location_id,
                "faction": stronghold.control,
                "defender_labels": [str(defender.army_name or f"{defender.army_faction} army").strip() for defender in defenders],
            }
        )
    return {"origin_h3": origin, "targets": targets}


@router.post("/me/actions")
def create_action(
    payload: ActionCreateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        army = _find_commander_army(session, commander_id)
        clock = _get_or_create_clock(session, for_update=True)
        current_action = _get_current_action_row(session, commander_id)
        if current_action is not None and current_action.state == "in_progress" and current_action.kind == "rout":
            raise HTTPException(status_code=409, detail="Army is routing; new orders unavailable until regroup.")
        action_params: dict[str, Any] = {}
        attack_target_name: str | None = None
        active_siege = _active_siege_for_besieger(session, army.army_id)
        sortie_stronghold, sortie_siege = _sortie_context_for_army(session, army)
        siege_to_preserve = False
        if payload.kind == "move":
            if clock.watch == int(Watch.NIGHT):
                raise HTTPException(status_code=400, detail="Move actions cannot be submitted during Night watch")
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
            # Forage orders may be issued at Night or Matin; they only start at Matin.
            if _army_is_under_siege(session, army):
                raise HTTPException(status_code=400, detail="Armies under siege cannot forage")
            if clock.watch not in {int(Watch.NIGHT), int(Watch.MATIN)}:
                action = Action(
                    commander_id=commander_id,
                    kind=payload.kind,
                    state="failed",
                    parameters_json=json.dumps(action_params),
                    accepted_at=datetime.now(timezone.utc),
                )
                session.add(action)
                session.flush()
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
        elif payload.kind == "attack":
            target_h3 = (payload.target_h3 or "").strip()
            if not target_h3:
                raise HTTPException(status_code=400, detail="target_h3 is required for attack actions")
            target_army_ref = (payload.target_army_id or "").strip()
            if not target_army_ref:
                raise HTTPException(status_code=400, detail="target_army_id is required for attack actions")
            target_army_id = _parse_army_ref(target_army_ref)
            target_army = session.get(Army, target_army_id)
            if target_army is None:
                raise HTTPException(status_code=400, detail={"message": "Unknown target_army_id", "target_army_id": target_army_ref})
            if not _army_has_live_detachments(target_army):
                raise HTTPException(status_code=400, detail="Cannot attack an army with no live detachments")
            if target_army.army_faction == army.army_faction:
                raise HTTPException(status_code=400, detail="Cannot attack a friendly army")
            if target_army.location_id != target_h3:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "target_army is not currently in target_h3",
                        "target_h3": target_h3,
                        "target_army_id": target_army_ref,
                    },
                )
            occupying_stronghold = _stronghold_at_h3(session, army.location_id)
            if occupying_stronghold is not None and sortie_siege is None:
                _, _, valid_sally_targets = _valid_stronghold_attack_targets(session, army)
                if target_army_id not in {candidate.army_id for candidate in valid_sally_targets}:
                    raise HTTPException(
                        status_code=400,
                        detail="Armies occupying strongholds may only attack adjacent enemy field armies",
                    )
            try:
                adjacent = set(h3.grid_ring(army.location_id, 1))
            except Exception:
                adjacent = set()
            target_stronghold = _stronghold_at_h3(session, target_h3)
            if active_siege is None and sortie_siege is None and target_h3 not in adjacent:
                raise HTTPException(status_code=400, detail="Attack target must be adjacent")
            if active_siege is None and sortie_siege is None and target_stronghold is not None:
                raise HTTPException(status_code=400, detail="Occupied strongholds must be besieged before they can be assaulted")
            if active_siege is not None:
                stronghold = session.get(Stronghold, active_siege.stronghold_id)
                if stronghold is None or target_h3 != stronghold.location_id:
                    raise HTTPException(status_code=400, detail="While besieging, attack targets must be inside the besieged stronghold")
                defender_ids = {defender.army_id for defender in _defender_armies_in_stronghold(session, stronghold, army.army_faction)}
                if target_army_id not in defender_ids:
                    raise HTTPException(status_code=400, detail="While besieging, only stronghold defenders may be attacked")
                siege_to_preserve = True
            elif sortie_siege is not None:
                participant_army_ids = {
                    participant.besieger_army_id
                    for participant in _active_siege_participants_for_siege(session, sortie_siege)
                }
                if target_army_id not in participant_army_ids:
                    raise HTTPException(status_code=400, detail="While under siege, only active besiegers may be attacked")
                if target_h3 not in adjacent:
                    raise HTTPException(status_code=400, detail="Sortie targets must be adjacent to the stronghold")
            attack_target_name = str(target_army.army_name or f"{target_army.army_faction} army").strip()
            action_params["target_h3"] = target_h3
            action_params["target_army_id"] = target_army_id
            active_actions = (
                session.query(Action)
                .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
                .all()
            )
            for existing in active_actions:
                existing.state = "cancelled"
        elif payload.kind == "besiege":
            if _army_is_in_stronghold(session, army):
                raise HTTPException(status_code=400, detail="Armies occupying strongholds cannot besiege other strongholds")
            target_stronghold_ref = (payload.target_stronghold_id or "").strip()
            if not target_stronghold_ref:
                raise HTTPException(status_code=400, detail="target_stronghold_id is required for besiege actions")
            stronghold_id = _parse_stronghold_ref(target_stronghold_ref)
            stronghold = session.get(Stronghold, stronghold_id)
            if stronghold is None:
                raise HTTPException(status_code=400, detail={"message": "Unknown target_stronghold_id", "target_stronghold_id": target_stronghold_ref})
            if stronghold.control == army.army_faction:
                raise HTTPException(status_code=400, detail="Cannot besiege a friendly stronghold")
            try:
                adjacent = set(h3.grid_ring(army.location_id, 1))
            except Exception:
                adjacent = set()
            if stronghold.location_id not in adjacent:
                raise HTTPException(status_code=400, detail="Besiege target must be adjacent")
            defenders = _defender_armies_in_stronghold(session, stronghold, army.army_faction)
            if not defenders:
                raise HTTPException(status_code=400, detail="Besiege target must contain enemy defenders")
            if active_siege is not None and int(active_siege.stronghold_id) != int(stronghold.stronghold_id):
                raise HTTPException(status_code=409, detail="This army is already maintaining a different siege")
            existing_siege = _active_siege_for_stronghold(session, stronghold.stronghold_id)
            if existing_siege is not None:
                besieger_faction = _active_siege_faction(session, existing_siege)
                if besieger_faction and besieger_faction != army.army_faction:
                    raise HTTPException(status_code=409, detail="That stronghold is already under siege by another faction")
            action_params["target_stronghold_id"] = stronghold.stronghold_id
            action_params["target_h3"] = stronghold.location_id
            action_params["target_stronghold_name"] = stronghold.stronghold_name
            active_actions = (
                session.query(Action)
                .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
                .all()
            )
            for existing in active_actions:
                existing.state = "cancelled"

        if active_siege is not None and payload.kind in {"move", "forage"}:
            # Replace the action row now. Movement ends siege duty only when its
            # first progress is consumed; forage never ends the authoritative
            # SiegeParticipant relationship.
            active_besiege_actions = (
                session.query(Action)
                .filter(
                    Action.commander_id == commander_id,
                    Action.kind == "besiege",
                    Action.state.in_(ACTIVE_ACTION_STATES),
                )
                .all()
            )
            for existing in active_besiege_actions:
                existing.state = "cancelled"

        action = Action(
            commander_id=commander_id,
            kind=payload.kind,
            state="queued",
            parameters_json=json.dumps(action_params),
            accepted_at=datetime.now(timezone.utc),
        )
        session.add(action)

        if payload.kind == "forage":
            _start_action_now_if_valid(session, action, army, clock)
        elif payload.kind == "attack":
            _start_action_now_if_valid(session, action, army, clock)
        elif payload.kind == "besiege":
            # Siege orders become effective during watch execution. An existing
            # matching participant remains effective while this order is pending.
            pass
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

        if payload.kind == "attack" and action.state in ACTIVE_ACTION_STATES:
            if siege_to_preserve and active_siege is not None:
                stronghold = session.get(Stronghold, active_siege.stronghold_id)
                target_name = stronghold.stronghold_name if stronghold is not None else "stronghold"
                alert_message = f"Assault ordered against {target_name}."
            else:
                target_name = attack_target_name or "enemy army"
                alert_message = f"Attack ordered against {target_name}."
            _create_alert(
                session,
                recipient_commander_id=commander_id,
                alert_type="action",
                signal_kind="event",
                category="orders",
                importance="normal",
                message=alert_message,
                created_day=clock.day,
                created_watch=clock.watch,
            )
        if payload.kind == "besiege" and action.state in ACTIVE_ACTION_STATES:
            _create_alert(
                session,
                recipient_commander_id=commander_id,
                alert_type="action",
                signal_kind="event",
                category="orders",
                importance="normal",
                message=f"Siege ordered against {action_params.get('target_stronghold_name', 'stronghold')}.",
                created_day=clock.day,
                created_watch=clock.watch,
            )

        session.flush()
        return {
            "action_id": _action_ref(action.action_id),
            "kind": action.kind,
            "state": action.state,
            "accepted_at": action.accepted_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    return _run_idempotent_mutation(
        session,
        actor_scope=f"commander:{commander_id}",
        route="create-action",
        idempotency_key=idempotency_key,
        payload=payload,
        operation=operation,
    )


@router.post("/me/actions/plan")
def plan_actions(
    payload: ActionPlanRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        army = _find_commander_army(session, commander_id)
        clock = _get_or_create_clock(session, for_update=True)
        current_action = _get_current_action_row(session, commander_id)
        if current_action is not None and current_action.state == "in_progress" and current_action.kind == "rout":
            raise HTTPException(status_code=409, detail="Army is routing; new orders unavailable until regroup.")
        path = [str(cell).strip() for cell in payload.path if str(cell).strip()]
        if payload.kind == "march":
            total_watch_cost = _path_watches_for_army(session, army, army.location_id, path)
            available_budget = _remaining_march_watch_budget_for_watch(
                int(clock.watch),
                army,
                _forced_march_enabled_for_army(session, army),
            )
            if total_watch_cost > available_budget:
                raise HTTPException(status_code=400, detail="March path exceeds remaining watch budget for this day.")
        created_actions, cancelled_count, cancelled_by_kind = _apply_plan(
            session,
            commander_id=commander_id,
            army=army,
            clock=clock,
            kind=payload.kind,
            path=path,
            now=datetime.now(timezone.utc),
            disable_follow_road=False,
        )
        cancelled_note = _cancellation_narrative(cancelled_by_kind)
        if payload.kind == "march" and not path:
            alert_message = f"Halt ordered, {cancelled_note}." if cancelled_note else "Halt ordered."
        elif payload.kind == "forage":
            alert_message = f"Forage ordered, {cancelled_note}." if cancelled_note else "Forage ordered."
        else:
            march_text = f"{len(path)}-league march ordered"
            alert_message = f"{march_text}, {cancelled_note}." if cancelled_note else f"{march_text}."
        _create_alert(
            session,
            recipient_commander_id=commander_id,
            alert_type="action",
            signal_kind="event",
            category="orders",
            importance="normal",
            message=alert_message,
            created_day=clock.day,
            created_watch=clock.watch,
        )

        session.flush()
        return {
            "kind": payload.kind,
            "hold": payload.kind == "march" and len(created_actions) == 0,
            "cancelled_count": cancelled_count,
            "cancelled_queued_count": cancelled_count,
            "cancelled_by_kind": cancelled_by_kind,
            "created": [
                {
                    "action_id": _action_ref(action.action_id),
                    "kind": action.kind,
                    "state": action.state,
                }
                for action in created_actions
            ],
        }

    return _run_idempotent_mutation(
        session,
        actor_scope=f"commander:{commander_id}",
        route="plan-actions",
        idempotency_key=idempotency_key,
        payload=payload,
        operation=operation,
    )


@router.get("/me/orders/standing")
def get_my_standing_orders(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    standing = session.get(StandingOrder, commander_id)
    return _serialize_standing_orders(standing)


@router.post("/me/orders/standing/follow-road")
def set_follow_road_standing_order(
    payload: StandingFollowRoadUpdateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        standing = _get_or_create_standing_order(session, commander_id)
        clock = _get_or_create_clock(session, for_update=True)
        standing.follow_road_enabled = bool(payload.enabled)
        if payload.enabled:
            standing.last_report = "Standing order issued: follow road."
            standing.last_report_day = None
            standing.last_report_watch = None
            _create_alert(
                session,
                recipient_commander_id=commander_id,
                alert_type="action",
                signal_kind="event",
                category="standing-order",
                importance="normal",
                message="Standing order issued: follow road.",
                created_day=clock.day,
                created_watch=clock.watch,
            )
        else:
            standing.last_report = "Standing order rescinded: follow road."
            standing.last_report_day = clock.day
            standing.last_report_watch = clock.watch
            _create_alert(
                session,
                recipient_commander_id=commander_id,
                alert_type="action",
                signal_kind="event",
                category="standing-order",
                importance="normal",
                message="Standing order rescinded: follow road.",
                created_day=clock.day,
                created_watch=clock.watch,
            )
        standing.updated_at = datetime.now(timezone.utc)
        return _serialize_standing_orders(standing)

    return _run_world_mutation(session, operation)


@router.post("/me/orders/standing/forced-march")
def set_forced_march_standing_order(
    payload: StandingFollowRoadUpdateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        army = _find_commander_army(session, commander_id)
        clock = _get_or_create_clock(session, for_update=True)
        current_action = _get_current_action_row(session, commander_id)
        if current_action is not None and current_action.state == "in_progress" and current_action.kind == "rout":
            raise HTTPException(status_code=409, detail="Army is routing; new orders unavailable until regroup.")
        standing = _get_or_create_standing_order(session, commander_id)
        requested_enabled = bool(payload.enabled)
        current_enabled = bool(standing.forced_march_enabled)
        _ = army
        if (
            current_enabled
            and not requested_enabled
            and _forced_march_is_locked_for_watch(int(clock.watch))
        ):
            raise HTTPException(status_code=400, detail="Forced march cannot be manually disabled in this watch.")
        if current_enabled == requested_enabled:
            return _serialize_standing_orders(standing)
        standing.forced_march_enabled = requested_enabled
        standing.updated_at = datetime.now(timezone.utc)
        if requested_enabled:
            _create_alert(
                session,
                recipient_commander_id=commander_id,
                alert_type="action",
                signal_kind="event",
                category="standing-order",
                importance="normal",
                message="Standing order issued: forced march.",
                created_day=clock.day,
                created_watch=clock.watch,
            )
        return _serialize_standing_orders(standing)

    return _run_world_mutation(session, operation)


@router.get("/correspondents")
def list_correspondents(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    armies_by_commander_id = {
        int(army.commander_id): str(army.army_faction or "").strip()
        for army in session.query(Army).filter(Army.commander_id.is_not(None)).all()
        if army.commander_id is not None
    }
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
            "faction": armies_by_commander_id.get(int(commander.commander_id), ""),
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
    return _serialize_action(session, current, commander_id)


@router.get("/me/alerts")
def list_alerts(
    limit: int = Query(default=50, ge=1, le=200),
    unread_only: bool = Query(default=False),
    after_id: str | None = Query(default=None),
    before_id: str | None = Query(default=None),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    if after_id and before_id:
        raise HTTPException(status_code=400, detail="after_id and before_id are mutually exclusive")
    clock = _get_or_create_clock(session)
    query = session.query(Alert, AlertRecipient).join(
        AlertRecipient, AlertRecipient.alert_id == Alert.alert_id
    ).filter(
        AlertRecipient.commander_id == commander_id,
        AlertRecipient.available_tick <= clock.world_tick,
        or_(
            Alert.signal_kind != "state",
            and_(
                Alert.signal_kind == "state",
                Alert.created_day == clock.day,
                Alert.created_watch == clock.watch,
            ),
        ),
    )
    if unread_only:
        query = query.filter(AlertRecipient.read_at.is_(None))
    if after_id:
        query = query.filter(Alert.alert_id > _parse_alert_ref(after_id))
        rows = query.order_by(Alert.alert_id.asc()).limit(limit + 1).all()
    else:
        if before_id:
            query = query.filter(Alert.alert_id < _parse_alert_ref(before_id))
        rows = query.order_by(Alert.alert_id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_serialize_alert(alert, receipt) for alert, receipt in rows],
        "has_more": has_more,
        "next_after_id": _alert_ref(rows[-1][0].alert_id) if after_id and rows else None,
        "next_before_id": _alert_ref(rows[-1][0].alert_id) if not after_id and rows else None,
    }


@router.post("/me/alerts/ack-delivered")
def acknowledge_alert_delivery(
    payload: AlertIdsRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    alert_ids = [_parse_alert_ref(value) for value in payload.alert_ids]

    def operation():
        now = datetime.now(timezone.utc)
        receipts = session.query(AlertRecipient).filter(
            AlertRecipient.commander_id == commander_id,
            AlertRecipient.alert_id.in_(alert_ids),
        ).all()
        for receipt in receipts:
            if receipt.delivered_at is None:
                receipt.delivered_at = now
        return {"acknowledged": len(receipts)}

    return _run_world_mutation(session, operation)


@router.post("/me/alerts/{alert_id}/read")
def mark_alert_read(
    alert_id: str,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    alert_pk = _parse_alert_ref(alert_id)

    def operation():
        receipt = session.get(AlertRecipient, (alert_pk, commander_id))
        if receipt is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        clock = _get_or_create_clock(session, for_update=True)
        if receipt.available_tick > clock.world_tick:
            raise HTTPException(status_code=404, detail="Alert not delivered yet")
        now = datetime.now(timezone.utc)
        receipt.delivered_at = receipt.delivered_at or now
        receipt.read_at = receipt.read_at or now
        return {"id": _alert_ref(alert_pk), "is_read": True, "read_at": receipt.read_at.isoformat()}

    return _run_world_mutation(session, operation)


@router.post("/me/actions/{action_id}/cancel")
def cancel_action(
    action_id: str,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        action_pk = _parse_action_ref(action_id)
        action = session.get(Action, action_pk)
        if action is None or action.commander_id != commander_id:
            raise HTTPException(status_code=404, detail="Action not found")
        if action.state not in ACTIVE_ACTION_STATES:
            raise HTTPException(status_code=409, detail="Action cannot be cancelled in current state")

        action.state = "cancelled"
        return {
            "action_id": _action_ref(action.action_id),
            "state": action.state,
        }

    return _run_idempotent_mutation(
        session,
        actor_scope=f"commander:{commander_id}",
        route=f"cancel-action:{action_id}",
        idempotency_key=idempotency_key,
        payload={"action_id": action_id},
        operation=operation,
    )


@router.post("/me/messages")
def send_message(
    payload: MessageCreateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    def operation():
        _lock_commander_scope(session, commander_id)
        sender = session.get(Commander, commander_id)
        if sender is None:
            raise HTTPException(status_code=404, detail="Sender commander not found")

        recipient_id = _parse_commander_ref(payload.recipient_id)
        recipient = session.get(Commander, recipient_id)
        if recipient is None:
            raise HTTPException(status_code=404, detail="Recipient not found")

        clock = _get_or_create_clock(session, for_update=True)
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
        _create_alert(
            session,
            recipient_commander_id=commander_id,
            alert_type="report",
            signal_kind="event",
            category="messages",
            importance="normal",
            message=f"Letter sent to {_commander_display_name(recipient)}.",
            created_day=clock.day,
            created_watch=clock.watch,
        )
        session.flush()

        return {
            "message_id": _message_ref(message.message_id),
            "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
            "status": message.status,
        }

    return _run_idempotent_mutation(
        session,
        actor_scope=f"commander:{commander_id}",
        route="send-message",
        idempotency_key=idempotency_key,
        payload=payload,
        operation=operation,
    )


@router.get("/me/messages")
def list_messages(
    unread_only: bool = Query(default=False),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    clock = _get_or_create_clock(session)
    delivered_incoming = and_(
        Message.recipient_id == commander_id,
        Message.status == "received",
        _is_delivered_filter(clock.day, clock.watch),
    )
    outgoing = Message.sender_commander_id == commander_id
    query = session.query(Message).filter(or_(delivered_incoming, outgoing))
    query = query.options(joinedload(Message.sender_commander), joinedload(Message.recipient))
    if unread_only:
        query = query.filter(delivered_incoming, Message.is_read.is_(False))

    messages = query.order_by(
        Message.sent_tick.desc(),
        Message.message_id.desc(),
    ).all()

    response = []
    for message in messages:
        if message.sender_commander_id == commander_id:
            response.append(
                {
                    "id": _message_ref(message.message_id),
                    "direction": "sent",
                    "to": {"name": _message_recipient_display_name(message)},
                    "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
                    "is_read": True,
                }
            )
        else:
            response.append({
                "id": _message_ref(message.message_id),
                "direction": "received",
                "from": {"name": _message_sender_display_name(message)},
                "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
                "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
                "is_read": message.is_read,
            })
    return response


@router.get("/me/messages/{message_id}")
def get_message(
    message_id: str,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    def operation():
        message_pk = _parse_message_ref(message_id)
        message = (
            session.query(Message)
            .options(joinedload(Message.recipient))
            .filter(Message.message_id == message_pk)
            .one_or_none()
        )
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found")
        if message.sender_commander_id == commander_id:
            return {
                "id": _message_ref(message.message_id),
                "direction": "sent",
                "to": {"name": _message_recipient_display_name(message)},
                "content": message.content,
                "priority": message.priority,
                "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
                "is_read": True,
            }
        if message.recipient_id != commander_id:
            raise HTTPException(status_code=404, detail="Message not found")
        if message.status != "received":
            if message.status == "lost":
                raise HTTPException(status_code=404, detail="Message was lost in transit")
            raise HTTPException(status_code=404, detail="Message not delivered yet")

        clock = _get_or_create_clock(session, for_update=True)
        if message.delivery_tick > clock.world_tick:
            raise HTTPException(status_code=404, detail="Message not delivered yet")

        if not message.is_read:
            message.is_read = True

        return {
            "id": _message_ref(message.message_id),
            "direction": "received",
            "from": {"name": _message_sender_display_name(message)},
            "content": message.content,
            "priority": message.priority,
            "sent_watch": _to_watch_stamp(message.sent_day, message.sent_watch),
            "delivered_watch": _to_watch_stamp(message.delivery_day, message.delivery_watch),
            "is_read": message.is_read,
        }

    return _run_world_mutation(session, operation)
