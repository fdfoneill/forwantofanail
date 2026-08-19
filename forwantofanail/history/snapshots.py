from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy.orm import Session, joinedload

from forwantofanail.core.models import (
    Army,
    GameClock,
    Siege,
    Stronghold,
    WorldHistoryEvent,
    WorldSnapshot,
)


SNAPSHOT_SCHEMA_VERSION = 1
HISTORY_EVENT_KINDS = {
    "battle",
    "stronghold_conquest",
    "siege_started",
    "siege_ended",
    "army_created",
    "army_destroyed",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def army_history_payload(army: Army) -> dict[str, Any]:
    commander = army.commander
    commander_name = ""
    if commander is not None:
        commander_name = " ".join(
            part for part in [str(commander.commander_title or "").strip(), str(commander.commander_name or "").strip()] if part
        )
    return {
        "army_id": int(army.army_id),
        "name": str(army.army_name or ""),
        "faction": str(army.army_faction or ""),
        "location_h3": str(army.location_id or ""),
        "commander_id": int(army.commander_id) if army.commander_id is not None else None,
        "commander_name": commander_name,
        "is_garrison": bool(army.is_garrison),
        "garrison_stronghold_id": int(army.garrison_stronghold_id) if army.garrison_stronghold_id is not None else None,
        "strength": sum(max(0, int(det.warrior_count or 0)) for det in army.detachments),
        "supply": max(0, int(army.army_supply or 0)),
        "morale": int(army.army_morale or 0),
    }


def _world_state(session: Session, clock: GameClock) -> dict[str, Any]:
    armies = (
        session.query(Army)
        .options(joinedload(Army.commander), joinedload(Army.detachments))
        .order_by(Army.army_id.asc())
        .all()
    )
    active_sieges = (
        session.query(Siege)
        .options(joinedload(Siege.participants))
        .filter(Siege.state == "active")
        .order_by(Siege.siege_id.asc())
        .all()
    )
    siege_by_stronghold = {int(siege.stronghold_id): siege for siege in active_sieges}
    strongholds = session.query(Stronghold).order_by(Stronghold.stronghold_id.asc()).all()
    stronghold_rows = []
    for stronghold in strongholds:
        siege = siege_by_stronghold.get(int(stronghold.stronghold_id))
        siege_payload = None
        if siege is not None:
            siege_payload = {
                "siege_id": int(siege.siege_id),
                "state": "active",
                "started_day": int(siege.started_day),
                "started_watch": int(siege.started_watch),
                "lead_besieger_army_id": int(siege.besieger_army_id),
                "lead_besieger_commander_id": (
                    int(siege.besieger_commander_id) if siege.besieger_commander_id is not None else None
                ),
                "matin_ticks_elapsed": int(siege.matin_ticks_elapsed or 0),
                "current_resistance": float(siege.current_resistance or 0.0),
                "max_resistance": float(siege.max_resistance or 0.0),
                "gates_open": bool(siege.gates_open),
                "participant_army_ids": sorted(
                    int(participant.besieger_army_id)
                    for participant in siege.participants
                    if participant.state == "active"
                ),
            }
        stronghold_rows.append(
            {
                "stronghold_id": int(stronghold.stronghold_id),
                "name": str(stronghold.stronghold_name or ""),
                "type": str(stronghold.stronghold_type or ""),
                "location_h3": str(stronghold.location_id or ""),
                "controller": str(stronghold.control or ""),
                "siege": siege_payload,
            }
        )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "world_tick": int(clock.world_tick),
        "day": int(clock.day),
        "watch": int(clock.watch),
        "armies": [army_history_payload(army) for army in armies],
        "strongholds": stronghold_rows,
    }


def capture_world_snapshot(session: Session, clock: GameClock, *, is_final: bool = False) -> WorldSnapshot:
    session.flush()
    if int(clock.world_tick) < 0:
        raise ValueError("world_tick must be non-negative")
    state_json = canonical_json(_world_state(session, clock))
    snapshot = session.get(WorldSnapshot, int(clock.world_tick))
    now = datetime.now(timezone.utc)
    if snapshot is None:
        snapshot = WorldSnapshot(
            world_tick=int(clock.world_tick),
            day=int(clock.day),
            watch=int(clock.watch),
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            state_json=state_json,
            is_final=bool(is_final),
            captured_at=now,
        )
        session.add(snapshot)
    else:
        if snapshot.is_final:
            return snapshot
        snapshot.day = int(clock.day)
        snapshot.watch = int(clock.watch)
        snapshot.schema_version = SNAPSHOT_SCHEMA_VERSION
        snapshot.state_json = state_json
        snapshot.is_final = bool(is_final)
        snapshot.captured_at = now
    return snapshot


def record_history_event(
    session: Session,
    *,
    event_key: str,
    world_tick: int,
    event_kind: str,
    location_id: str | None,
    payload: dict[str, Any],
) -> WorldHistoryEvent:
    normalized_kind = str(event_kind or "").strip().lower()
    if normalized_kind not in HISTORY_EVENT_KINDS:
        raise ValueError(f"Unsupported history event kind: {event_kind}")
    normalized_key = str(event_key or "").strip()
    if not normalized_key:
        raise ValueError("event_key is required")
    for pending in session.new:
        if isinstance(pending, WorldHistoryEvent) and pending.event_key == normalized_key:
            return pending
    existing = session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_key == normalized_key).first()
    if existing is not None:
        return existing
    event = WorldHistoryEvent(
        event_key=normalized_key,
        world_tick=int(world_tick),
        event_kind=normalized_kind,
        location_id=str(location_id).strip() if location_id else None,
        payload_json=canonical_json(payload),
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)
    return event
