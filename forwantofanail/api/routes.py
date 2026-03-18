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
    ActionPlanRequest,
    LoginRequest,
    MessageCreateRequest,
    StandingFollowRoadUpdateRequest,
    TimeAdvanceRequest,
)
from forwantofanail.core.database import create_session
from forwantofanail.core.models import (
    Action,
    Alert,
    Army,
    AuthToken,
    Commander,
    GameClock,
    Location,
    Message,
    Movement,
    StandingOrder,
    Stronghold,
    TerrainType,
)
from forwantofanail.mechanics.movement import (
    calculate_move_watches,
    calculate_move_watches_from_origin,
    list_valid_destinations,
    list_valid_destinations_from_origin,
)
from forwantofanail.mechanics.supply import (
    consume_supply_for_all_armies,
    noncombatant_count,
    supply_stats,
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
SCENARIO_EPOCH = date(1410, 5, 20)
MESSAGE_LOSS_PROBABILITY = 0.0
MAX_FOLLOW_ROAD_STEPS = 4
ALERT_TYPES = {"world event", "action", "report", "violence", "morale"}
BATTLE_ALERT_IMPORTANCE = "high"


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


def _commander_display_name(commander: Commander) -> str:
    title = (commander.commander_title or "").strip()
    name = (commander.commander_name or "").strip()
    return f"{title} {name}".strip() if title else name


def _message_sender_display_name(message: Message) -> str:
    if message.sender_commander is not None:
        return _commander_display_name(message.sender_commander)
    return message.sender_name


def _alert_ref(alert_id: int) -> str:
    return f"alt_{alert_id}"


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
    other_armies: list[dict[str, Any]],
) -> str:
    first_army = other_armies[0] if other_armies else None
    if first_army and stronghold_name:
        return f"{_cell_army_display_name(first_army)} occupying {stronghold_name}"
    if first_army:
        return _cell_army_display_name(first_army)
    if stronghold_name:
        return stronghold_name
    if terrain_type.strip().lower() == "river" and has_road:
        return "bridge"
    if has_road:
        return "road"
    return terrain_type


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
        "calendar_date": _scenario_date_for_day(clock.day).isoformat(),
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
) -> Alert:
    normalized_type = alert_type.strip().lower()
    if normalized_type not in ALERT_TYPES:
        normalized_type = "report"
    normalized_signal_kind = signal_kind.strip().lower()
    if normalized_signal_kind not in {"event", "state"}:
        normalized_signal_kind = "event"
    alert = Alert(
        recipient_commander_id=recipient_commander_id,
        alert_type=normalized_type,
        signal_kind=normalized_signal_kind,
        category=(category or "general").strip().lower(),
        importance=(importance or "normal").strip().lower(),
        message=message,
        payload_json=json.dumps(payload or {}),
        created_day=created_day,
        created_watch=created_watch,
        delivered_day=delivered_day if delivered_day is not None else created_day,
        delivered_watch=delivered_watch if delivered_watch is not None else created_watch,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(alert)
    return alert


def _serialize_alert(alert: Alert) -> dict[str, Any]:
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
        "is_read": alert.is_read,
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


def _apply_random_warrior_loss(army: Army, percent: float) -> int:
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


def _describe_location_by_nearest_strongholds(session: Session, location_h3: str) -> str:
    strongholds = session.query(Stronghold).all()
    if not strongholds:
        return "at an unknown location"
    ranked = sorted(
        strongholds,
        key=lambda sh: (
            max(0, _grid_distance(location_h3, sh.location_id)),
            int(sh.stronghold_id),
        ),
    )
    if len(ranked) == 1:
        return f"near {ranked[0].stronghold_name}"
    return f"between {ranked[0].stronghold_name} and {ranked[1].stronghold_name}"


def _run_morale_test_for_army(
    session: Session,
    *,
    army: Army,
    clock: GameClock,
    category: str,
) -> None:
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
        lost_warriors = _apply_random_warrior_loss(army, 0.30)
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
        lost_warriors = _apply_random_warrior_loss(army, 0.20)
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
        lost_warriors = _apply_random_warrior_loss(army, 0.10)
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
                descriptor = _describe_location_by_nearest_strongholds(session, army.location_id)
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
        f"{stronghold.stronghold_name} was conquered by {new_faction} "
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


def _remaining_march_steps_for_watch(watch: int) -> int:
    # March actions can complete at night but not in the same watch they are set.
    # Thus:
    # watch 1 -> 4 possible completions (2,3,4,0), watch 2 -> 3, watch 3 -> 2, watch 4 -> 1.
    # Night submissions are allowed only for full 4-step march plans.
    if watch <= int(Watch.MATIN):
        return 4
    return max(0, 5 - int(watch))


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


def _forage_supply_gain_for_army(session: Session, army: Army) -> tuple[int, list[Location]]:
    radius = _environs_radius_for_army(army)
    visible_h3 = list(h3.grid_disk(army.location_id, radius))
    locations = session.query(Location).filter(Location.location_id.in_(visible_h3)).all()
    gain = sum(max(int(location.settlement or 0), 0) * 2500 for location in locations)
    return gain, locations


def _serialize_standing_orders(standing: StandingOrder | None) -> dict[str, Any]:
    if standing is None:
        return {
            "follow_road": {
                "enabled": False,
                "last_report": None,
                "last_report_day": None,
                "last_report_watch": None,
            }
        }
    return {
        "follow_road": {
            "enabled": bool(standing.follow_road_enabled),
            "last_report": standing.last_report,
            "last_report_day": standing.last_report_day,
            "last_report_watch": standing.last_report_watch,
        }
    }


def _get_or_create_standing_order(session: Session, commander_id: int) -> StandingOrder:
    standing = session.get(StandingOrder, commander_id)
    if standing is not None:
        return standing
    standing = StandingOrder(
        commander_id=commander_id,
        follow_road_enabled=False,
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
) -> tuple[list[Action], int]:
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
            max_steps = _remaining_march_steps_for_watch(int(clock.watch))
            if len(path) > max_steps:
                raise HTTPException(
                    status_code=400,
                    detail=f"March path too long for current watch: max {max_steps} cells, got {len(path)}",
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

    cancelled_by_kind: dict[str, int] = {}
    for existing in active_actions:
        kind = (existing.kind or "unknown").strip().lower()
        cancelled_by_kind[kind] = cancelled_by_kind.get(kind, 0) + 1

    return created_actions, len(active_actions), cancelled_by_kind


def _auto_apply_follow_road_orders(session: Session, clock: GameClock) -> None:
    if clock.watch != int(Watch.NIGHT):
        return
    # Ensure movement rows created earlier in this same tick are visible to
    # previous-position lookup before building the next standing-order plan.
    session.flush()
    standing_rows = (
        session.query(StandingOrder)
        .filter(StandingOrder.follow_road_enabled.is_(True))
        .all()
    )
    for standing in standing_rows:
        army = session.query(Army).filter(Army.commander_id == standing.commander_id).first()
        if army is None:
            _set_standing_order_report(
                session,
                standing,
                clock=clock,
                message="Road march halted: no field army available for this commander.",
            )
            continue

        previous_h3 = _latest_previous_location_for_army(session, army)
        if not previous_h3:
            _set_standing_order_report(
                session,
                standing,
                clock=clock,
                message="Road march halted: previous position unknown; new orders needed.",
            )
            continue

        path: list[str] = []
        current_h3 = army.location_id
        last_h3 = previous_h3
        stop_reason: str | None = None
        stop_reason_code: str | None = None
        for _ in range(MAX_FOLLOW_ROAD_STEPS):
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
            path.append(next_h3)
            last_h3, current_h3 = current_h3, next_h3

        if not path:
            _set_standing_order_report(
                session,
                standing,
                clock=clock,
                message=stop_reason or "Road march halted: unable to continue.",
            )
            continue

        try:
            _apply_plan(
                session,
                commander_id=standing.commander_id,
                army=army,
                clock=clock,
                kind="march",
                path=path,
                now=datetime.now(timezone.utc),
                allow_partial_night_march=True,
            )
        except HTTPException as exc:
            _set_standing_order_report(
                session,
                standing,
                clock=clock,
                message=f"Road march halted: {exc.detail}",
            )
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
        if stop_reason:
            # Only alert for dead-end/crossroads when it is the current
            # end-of-march cell (i.e., no next step could be staged at all).
            if not (stop_reason_code in {"crossroads", "dead_end"} and path):
                _set_standing_order_report(
                    session,
                    standing,
                    clock=clock,
                    message=stop_reason,
                )
        else:
            standing.updated_at = datetime.now(timezone.utc)


def _infantry_count(army: Army) -> int:
    return sum(int(det.warrior_count or 0) for det in army.detachments if not det.is_cavalry)


def _cavalry_count(army: Army) -> int:
    return sum(int(det.warrior_count or 0) for det in army.detachments if det.is_cavalry)


def _effective_strength(army: Army) -> int:
    return _infantry_count(army) + (2 * _cavalry_count(army))


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
        session.query(Army)
        .filter(
            Army.location_id == destination_h3,
            Army.army_id != moving_army.army_id,
            Army.army_faction != moving_army.army_faction,
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
    destination_location = session.get(Location, destination_h3)
    if destination_location is None:
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
    return True


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
    active = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
        .all()
    )
    for row in active:
        row.state = "cancelled"
    watches_needed = _path_watches_for_army(session, army, army.location_id, path)
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


def _resolve_due_attack_battles(session: Session, clock: GameClock, due_attack_actions: list[Action]) -> dict[str, int]:
    if not due_attack_actions:
        return {"completed": 0, "failed": 0}

    action_by_id: dict[int, Action] = {action.action_id: action for action in due_attack_actions}
    attacker_army_by_action_id: dict[int, Army] = {}
    target_army_id_by_action_id: dict[int, int] = {}
    target_h3_by_action_id: dict[int, str] = {}
    edges: list[tuple[int, int, int]] = []  # (action_id, attacker_army_id, target_army_id)
    failed = 0
    completed = 0

    for action in due_attack_actions:
        attacker = session.query(Army).filter(Army.commander_id == action.commander_id).first()
        if attacker is None:
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
        attacker_army_by_action_id[action.action_id] = attacker
        target_army_id_by_action_id[action.action_id] = target_army_id
        target_h3_by_action_id[action.action_id] = target_h3
        edges.append((action.action_id, attacker.army_id, target_army_id))

    if not edges:
        return {"completed": completed, "failed": failed}

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
        for action_id, attacker_id, target_id in edges:
            if attacker_id in participant_ids and target_id in participant_ids:
                action_ids_in_component.add(action_id)
                attacker_ids.add(attacker_id)
                incoming_by_target[target_id].append(action_id)

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

        side_strength: dict[str, int] = {faction: sum(_effective_strength(army) for army in armies) for faction, armies in sides.items()}
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
            highest_enemy_morale = max((_clamp_morale(enemy.army_morale) for f in enemy_factions for enemy in sides.get(f, [])), default=2)
            morale_adv = max(0, _clamp_morale(army.army_morale) - highest_enemy_morale)
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
            out_of_formation = 1 if (active_kind is not None and active_kind != "attack") else 0
            target_h3 = None
            if is_attacker:
                for action_id in edge_action_ids_by_node.get(army.army_id, set()):
                    if action_id in action_ids_in_component:
                        target_h3 = target_h3_by_action_id.get(action_id)
                        if target_h3:
                            break
            else:
                incoming = incoming_by_target.get(army.army_id, [])
                if incoming:
                    target_h3 = target_h3_by_action_id.get(incoming[0])
            rough_terrain = 0
            if target_h3:
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
            if is_attacker:
                target_army_id = None
                for action_id in edge_action_ids_by_node.get(army.army_id, set()):
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
            }
            roll = random.randint(1, 6) + random.randint(1, 6)
            final_roll = roll + sum(mods.values())
            army_modifiers[army.army_id] = mods
            army_final_roll[army.army_id] = final_roll
            prior = side_top_roll.get(faction)
            if prior is None or final_roll > prior:
                side_top_roll[faction] = final_roll
                side_top_army_id[faction] = army.army_id

        winner_faction = max(side_top_roll.keys(), key=lambda faction: side_top_roll[faction])
        winner_armies = sides[winner_faction]
        winner_top_army = session.get(Army, side_top_army_id[winner_faction]) if winner_faction in side_top_army_id else None

        casualties_by_army: dict[int, int] = {}
        morale_delta_by_army: dict[int, int] = defaultdict(int)
        retreat_by_army: dict[int, dict[str, Any]] = {}
        rout_by_army: dict[int, bool] = defaultdict(bool)

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
                if army.army_id in attacker_ids:
                    morale_delta_by_army[army.army_id] -= 1
            elif diff == 1:
                casualty_pct = 0.10
                if loser:
                    morale_delta_by_army[army.army_id] -= 1
            elif diff in {2, 3}:
                casualty_pct = 0.05 if winner else 0.10
                if loser:
                    morale_delta_by_army[army.army_id] -= 2
                if winner:
                    morale_delta_by_army[army.army_id] += 1
            elif diff in {4, 5}:
                casualty_pct = 0.05 if winner else 0.15
                if loser:
                    morale_delta_by_army[army.army_id] -= 2
                if winner:
                    morale_delta_by_army[army.army_id] += 2
            else:
                casualty_pct = 0.05 if winner else 0.20
                if loser:
                    morale_delta_by_army[army.army_id] -= 2
                if winner:
                    morale_delta_by_army[army.army_id] += 2
            casualties_by_army[army.army_id] = _apply_random_warrior_loss(army, casualty_pct)

            if loser:
                retreat_ok = _retreat_one_cell(session, army=army, winner_armies=winner_armies, clock=clock)
                retreat_by_army[army.army_id] = {"retreated": retreat_ok}
                if not retreat_ok:
                    lost_w, lost_s = _halve_army(session, army)
                    retreat_by_army[army.army_id] = {
                        "retreated": False,
                        "fallback_halved": True,
                        "lost_warriors": lost_w,
                        "lost_supply": lost_s,
                    }
                else:
                    check = random.randint(1, 6) + random.randint(1, 6)
                    if check > _clamp_morale(army.army_morale):
                        rout_by_army[army.army_id] = True
                        supply_loss_pct = random.randint(1, 6) * 0.10
                        lost_supply = _apply_supply_loss(army, supply_loss_pct)
                        if winner_top_army is not None:
                            winner_top_army.army_supply = int(winner_top_army.army_supply or 0) + lost_supply
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
            if action.state == "in_progress":
                action.state = "completed"
                completed += 1

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
                for action_id in edge_action_ids_by_node.get(army.army_id, set()):
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
            ratio = _ratio_label(own_side_strength, enemy_side_strength)
            battle_position: list[str] = []
            if ratio:
                battle_position.append(f"with {ratio} numerical superiority")
            if int(own_mods.get("morale_advantage", 0) or 0) > 0:
                battle_position.append("with superior morale")
            if int(own_mods.get("chosen_battlefield", 0) or 0) > 0:
                battle_position.append("holding chosen ground")
            if int(own_mods.get("surprise", 0) or 0) > 0:
                battle_position.append("with surprise")
            if int(own_mods.get("rough_terrain", 0) or 0) < 0:
                battle_position.append("in rough terrain")
            if int(own_mods.get("undersupplied", 0) or 0) < 0:
                battle_position.append("while undersupplied")
            if int(own_mods.get("out_of_formation", 0) or 0) < 0:
                battle_position.append("out of formation")

            if army.army_id in attacker_ids:
                opener = f"BATTLE! {army_name} attacks {enemy_display}"
            else:
                opener = f"BATTLE! {enemy_display} attacks {army_name}"
            if battle_position:
                opener = f"{opener} {' and '.join(battle_position)}"
            opener = f"{opener}."

            casualties = int(casualties_by_army.get(army.army_id, 0) or 0)
            morale_delta = int(morale_delta_by_army.get(army.army_id, 0) or 0)
            if morale_delta > 0:
                morale_text = "army morale increased."
            elif morale_delta < 0:
                morale_text = "army morale decreased."
            else:
                morale_text = "army morale held."
            enemy_routed = any(bool(rout_by_army.get(other.army_id, False)) for other in enemy_armies)
            rout_text = " Enemy routed." if enemy_routed else ""
            message = (
                f"{opener} {winner_faction.upper()} VICTORY. "
                f"{casualties} {own_faction} warriors slain, {morale_text}{rout_text}"
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
                    "morale_delta": morale_delta_by_army.get(army.army_id, 0),
                    "retreat": retreat_by_army.get(army.army_id, {}),
                    "rout": bool(rout_by_army.get(army.army_id, False)),
                },
            )

    return {"completed": completed, "failed": failed}


def _start_action_now_if_valid(session: Session, action: Action, army: Army, clock: GameClock) -> bool:
    if action.kind == "move":
        if clock.watch == int(Watch.NIGHT):
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
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, clock.watch, watches_needed)
        return True

    if action.kind == "forage":
        if clock.watch == int(Watch.NIGHT):
            # Night submissions remain queued until at least Matin.
            return False
        # If execution skipped over Matin for any reason, start forage as-if at Matin.
        effective_start_watch = int(Watch.MATIN)
        action.started_day = clock.day
        action.started_watch = effective_start_watch
        action.state = "in_progress"
        # Forage duration is exactly 4 watch transitions from Matin start.
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, effective_start_watch, 4)
        return True

    if action.kind == "attack":
        if clock.watch == int(Watch.NIGHT):
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
        action.started_day = clock.day
        action.started_watch = clock.watch
        action.state = "in_progress"
        action.eta_day, action.eta_watch = _advance_day_watch(clock.day, clock.watch, 1)
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
    deferred_moves: list[tuple[Action, Army, str]] = []
    deferred_once: set[int] = set()

    # First, attempt to complete currently in-progress non-attack actions.
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
        if not _watch_is_at_or_after(clock.day, clock.watch, action.eta_day, action.eta_watch):
            continue

        if action.kind == "attack":
            due_attack_actions.append(action)
            continue

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
            if _is_enemy_occupied(session, destination_h3=destination_h3, moving_army=army):
                deferred_moves.append((action, army, destination_h3))
                deferred_once.add(action.action_id)
                continue
            destination_location = session.get(Location, destination_h3)
            if destination_location is None:
                action.state = "failed"
                failed += 1
                continue
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
            stronghold = (
                session.query(Stronghold)
                .filter(Stronghold.location_id == destination_h3)
                .first()
            )
            if stronghold is not None and stronghold.control != army.army_faction:
                previous_faction = stronghold.control
                stronghold.control = army.army_faction
                _emit_stronghold_conquest_alerts(
                    session,
                    stronghold=stronghold,
                    previous_faction=previous_faction,
                    new_faction=army.army_faction,
                    clock=clock,
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

        if action.kind == "rout":
            try:
                payload = json.loads(action.parameters_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            path = [str(h3_index).strip() for h3_index in (payload.get("path") or []) if str(h3_index).strip()]
            if path:
                destination_h3 = path[-1]
                destination_location = session.get(Location, destination_h3)
                if destination_location is not None:
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
            action.state = "completed"
            completed += 1
            continue

        action.state = "failed"
        failed += 1

    # One deferred pass for blocked moves: "back of the line" in same watch.
    for action, army, destination_h3 in deferred_moves:
        if action.state != "in_progress":
            continue
        if _is_enemy_occupied(session, destination_h3=destination_h3, moving_army=army):
            action.state = "failed"
            failed += 1
            continue
        destination_location = session.get(Location, destination_h3)
        if destination_location is None:
            action.state = "failed"
            failed += 1
            continue
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
        stronghold = (
            session.query(Stronghold)
            .filter(Stronghold.location_id == destination_h3)
            .first()
        )
        if stronghold is not None and stronghold.control != army.army_faction:
            previous_faction = stronghold.control
            stronghold.control = army.army_faction
            _emit_stronghold_conquest_alerts(
                session,
                stronghold=stronghold,
                previous_faction=previous_faction,
                new_faction=army.army_faction,
                clock=clock,
            )
        action.state = "completed"
        completed += 1

    # Resolve due attack battles after non-attack movement/forage/rout effects.
    battle_result = _resolve_due_attack_battles(session, clock, due_attack_actions)
    completed += int(battle_result.get("completed", 0))
    failed += int(battle_result.get("failed", 0))

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
        "morale": {
            "current": current_morale,
            "resting": resting_morale,
            "min": 2,
            "max": 12,
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
        .order_by(Army.army_id.asc())
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
        stronghold_name = stronghold.stronghold_name if stronghold else None
        terrain_type = terrain.terrain_name if terrain else "unknown"
        cells.append(
            {
                "h3": location.location_id,
                "terrain_type": terrain_type,
                "has_road": location.is_road,
                "cell_title": _cell_title(
                    terrain_type=terrain_type,
                    has_road=bool(location.is_road),
                    stronghold_name=stronghold_name,
                    other_armies=other_armies,
                ),
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
    for action in actions:
        if action.kind != "move":
            continue
        destination_h3 = _get_destination_h3(action)
        if destination_h3:
            remaining_moves.append(destination_h3)
    return {
        "remaining_moves": remaining_moves,
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
        message_result = _process_messages_tick(session, clock)
        tick_result = {"started": 0, "completed": 0, "failed": 0}
        if payload.execute_actions:
            tick_result = _execute_action_tick(session, clock)
            _auto_apply_follow_road_orders(session, clock)
            actions_started += tick_result["started"]
            actions_completed += tick_result["completed"]
            actions_failed += tick_result["failed"]
        supply_result = None
        if clock.watch == int(Watch.NIGHT):
            # Night supply checks run after action resolution so completed forage can replenish first.
            supply_result = consume_supply_for_all_armies(session)
            _emit_supply_alerts_after_consumption(session, clock)
        _emit_no_supply_state_alerts(session, clock)
        _emit_enemy_proximity_alerts(session, clock)
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
    standing_order = session.get(StandingOrder, commander_id)

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
        "itinerary": _serialize_remaining_itinerary(session, commander_id),
        "standing_orders": _serialize_standing_orders(standing_order),
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


@router.get("/me/actions/valid-next")
def get_valid_next_destinations(
    origin_h3: str | None = Query(default=None, description="Origin H3 to validate next movement from"),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    origin = (origin_h3 or army.location_id or "").strip()
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

    filtered = [h3_index for h3_index in valid if not _is_enemy_occupied(session, destination_h3=h3_index, moving_army=army)]
    return {"origin_h3": origin, "valid_destinations": sorted(filtered)}


@router.get("/me/actions/valid-attack")
def get_valid_attack_targets(
    origin_h3: str | None = Query(default=None, description="Origin H3 to validate attacks from"),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    origin = (origin_h3 or army.location_id or "").strip()
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
        .filter(
            Army.location_id.in_(list(neighbors)),
            Army.army_id != army.army_id,
            Army.army_faction != army.army_faction,
        )
        .order_by(Army.army_id.asc())
        .all()
    )
    targets = []
    for enemy in enemies:
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


@router.post("/me/actions")
def create_action(
    payload: ActionCreateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    clock = _get_or_create_clock(session)
    current_action = _get_current_action_row(session, commander_id)
    if current_action is not None and current_action.state == "in_progress" and current_action.kind == "rout":
        raise HTTPException(status_code=409, detail="Army is routing; new orders unavailable until regroup.")
    action_params: dict[str, Any] = {}
    attack_target_name: str | None = None
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
        if clock.watch not in {int(Watch.NIGHT), int(Watch.MATIN)}:
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
        try:
            adjacent = set(h3.grid_ring(army.location_id, 1))
        except Exception:
            adjacent = set()
        if target_h3 not in adjacent:
            raise HTTPException(status_code=400, detail="Attack target must be adjacent")
        attack_target_name = str(target_army.army_name or f"{target_army.army_faction} army").strip()
        action_params["target_h3"] = target_h3
        action_params["target_army_id"] = target_army_id
        # Attack preempts all existing queued and in-progress actions.
        active_actions = (
            session.query(Action)
            .filter(Action.commander_id == commander_id, Action.state.in_(ACTIVE_ACTION_STATES))
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
        if not _start_action_now_if_valid(session, action, army, clock) and clock.watch != int(Watch.NIGHT):
            action.state = "failed"
    elif payload.kind == "attack":
        if not _start_action_now_if_valid(session, action, army, clock) and clock.watch != int(Watch.NIGHT):
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

    if payload.kind == "attack" and action.state in ACTIVE_ACTION_STATES:
        target_name = attack_target_name or "enemy army"
        _create_alert(
            session,
            recipient_commander_id=commander_id,
            alert_type="action",
            signal_kind="event",
            category="orders",
            importance="normal",
            message=f"Attack ordered against {target_name}.",
            created_day=clock.day,
            created_watch=clock.watch,
        )

    session.commit()
    session.refresh(action)

    return {
        "action_id": _action_ref(action.action_id),
        "kind": action.kind,
        "state": action.state,
        "accepted_at": action.accepted_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


@router.post("/me/actions/plan")
def plan_actions(
    payload: ActionPlanRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    army = _find_commander_army(session, commander_id)
    clock = _get_or_create_clock(session)
    current_action = _get_current_action_row(session, commander_id)
    if current_action is not None and current_action.state == "in_progress" and current_action.kind == "rout":
        raise HTTPException(status_code=409, detail="Army is routing; new orders unavailable until regroup.")
    path = [str(cell).strip() for cell in payload.path if str(cell).strip()]
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

    session.commit()
    for action in created_actions:
        session.refresh(action)

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


@router.get("/me/orders/standing")
def get_my_standing_orders(
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    standing = _get_or_create_standing_order(session, commander_id)
    session.commit()
    session.refresh(standing)
    return _serialize_standing_orders(standing)


@router.post("/me/orders/standing/follow-road")
def set_follow_road_standing_order(
    payload: StandingFollowRoadUpdateRequest,
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    standing = _get_or_create_standing_order(session, commander_id)
    clock = _get_or_create_clock(session)
    standing.follow_road_enabled = bool(payload.enabled)
    if payload.enabled:
        standing.last_report = "Standing order active: follow road."
        standing.last_report_day = None
        standing.last_report_watch = None
        _create_alert(
            session,
            recipient_commander_id=commander_id,
            alert_type="action",
            signal_kind="event",
            category="standing-order",
            importance="normal",
            message="Standing order active: follow road.",
            created_day=clock.day,
            created_watch=clock.watch,
        )
    else:
        standing.last_report = "Standing order cancelled: follow road."
        standing.last_report_day = clock.day
        standing.last_report_watch = clock.watch
        _create_alert(
            session,
            recipient_commander_id=commander_id,
            alert_type="action",
            signal_kind="event",
            category="standing-order",
            importance="normal",
            message="Standing order cancelled: follow road.",
            created_day=clock.day,
            created_watch=clock.watch,
        )
    standing.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(standing)
    return _serialize_standing_orders(standing)


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


@router.get("/me/alerts")
def list_alerts(
    limit: int = Query(default=25, ge=1, le=200),
    unread_only: bool = Query(default=False),
    commander_id: int = Depends(_get_current_commander_id),
    session: Session = Depends(_get_session),
):
    clock = _get_or_create_clock(session)
    query = session.query(Alert).filter(
        or_(Alert.recipient_commander_id == commander_id, Alert.recipient_commander_id.is_(None)),
        or_(
            Alert.delivered_day < clock.day,
            and_(Alert.delivered_day == clock.day, Alert.delivered_watch <= clock.watch),
        )
    )
    if unread_only:
        query = query.filter(Alert.is_read.is_(False))
    alerts = (
        query.order_by(Alert.delivered_day.desc(), Alert.delivered_watch.desc(), Alert.alert_id.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_alert(alert) for alert in alerts]


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
