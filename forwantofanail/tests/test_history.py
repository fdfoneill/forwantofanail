from __future__ import annotations

from datetime import datetime, timezone
import json

import h3
import pytest

from forwantofanail.api import routes
from forwantofanail.api.schemas import TimeAdvanceRequest
from forwantofanail.core.database import create_session, reset_database_runtime
from forwantofanail.core.migrate_runtime_tables import migrate_runtime_tables
from forwantofanail.core.models import (
    Army,
    Commander,
    Detachment,
    GameClock,
    Location,
    Stronghold,
    TerrainType,
    WorldHistoryEvent,
    WorldSnapshot,
)
from forwantofanail.history.export import DEFAULT_CONFIG, export_history, schedule_events_for_frames, select_snapshots
from forwantofanail.history.snapshots import capture_world_snapshot, record_history_event


@pytest.fixture()
def history_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'history.db'}")
    monkeypatch.setenv("ADMIN_TOKEN", "history-admin")
    reset_database_runtime()
    migrate_runtime_tables()
    center = h3.latlng_to_cell(41.0, 29.0, 7)
    neighbors = sorted(h3.grid_ring(center, 1))
    session = create_session()
    try:
        session.add_all(
            [
                TerrainType(terrain_id=1, terrain_name="Land", speed_multiplier=1.0, scout_multiplier=1.0, is_water=False),
                TerrainType(terrain_id=2, terrain_name="Water", speed_multiplier=1.0, scout_multiplier=1.0, is_water=True),
            ]
        )
        session.add_all(
            [
                Location(location_id=center, terrain_id=1, region="Citadel", is_road=True, settlement=3),
                Location(location_id=neighbors[0], terrain_id=1, region="Citadel", is_road=True, settlement=1),
                Location(location_id=neighbors[1], terrain_id=2, region=None, is_road=False, settlement=0),
            ]
        )
        commander = Commander(commander_id=1, commander_name="Alpha", commander_title="Lord", commander_age=30)
        army = Army(
            army_id=1,
            location_id=center,
            army_name="Alpha Host",
            army_faction="Allakia",
            commander_id=1,
            army_supply=90,
            army_morale=8,
            army_resting_morale=8,
        )
        session.add_all(
            [
                commander,
                army,
                Detachment(detachment_id=1, detachment_name="Spears", army_id=1, warrior_count=120),
                Stronghold(
                    stronghold_id=1,
                    stronghold_name="Citadel",
                    stronghold_type="fortress",
                    location_id=center,
                    control="Allakia",
                    stronghold_threshold=0,
                ),
            ]
        )
        clock = session.get(GameClock, 1)
        clock.day = 1
        clock.watch = 1
        clock.world_tick = 0
        session.commit()
    finally:
        session.close()
    yield {"tmp_path": tmp_path, "center": center}
    reset_database_runtime()


def test_snapshot_serialization_is_complete_deterministic_and_final_is_immutable(history_db):
    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        first = capture_world_snapshot(session, clock)
        session.flush()
        first_json = first.state_json
        capture_world_snapshot(session, clock)
        assert first.state_json == first_json
        session.get(Army, 1).army_supply = 42
        capture_world_snapshot(session, clock, is_final=True)
        session.flush()
        state = json.loads(first.state_json)
        assert state["armies"][0]["strength"] == 120
        assert state["armies"][0]["commander_name"] == "Lord Alpha"
        assert state["armies"][0]["supply"] == 42
        assert state["strongholds"][0]["controller"] == "Allakia"
        assert first.state_json != first_json
        finalized_json = first.state_json
        session.get(Army, 1).army_supply = 1
        capture_world_snapshot(session, clock)
        assert first.state_json == finalized_json
    finally:
        session.rollback()
        session.close()


def test_multi_watch_advance_finalizes_departing_ticks_and_leaves_current_provisional(history_db):
    session = create_session()
    try:
        capture_world_snapshot(session, session.get(GameClock, 1))
        session.commit()
    finally:
        session.close()
    session = create_session()
    try:
        result = routes.advance_time_for_development(
            TimeAdvanceRequest(steps=2, execute_actions=False),
            session=session,
            x_admin_token="history-admin",
            idempotency_key="history-advance-1",
        )
        assert result["steps"] == 2
    finally:
        session.close()
    session = create_session()
    try:
        snapshots = session.query(WorldSnapshot).order_by(WorldSnapshot.world_tick).all()
        assert [row.world_tick for row in snapshots] == [0, 1, 2]
        assert [row.is_final for row in snapshots] == [True, True, False]
    finally:
        session.close()


def test_history_event_is_canonical_and_idempotent(history_db):
    session = create_session()
    try:
        first = record_history_event(
            session,
            event_key="battle:0:test",
            world_tick=0,
            event_kind="battle",
            location_id=history_db["center"],
            payload={"winner": "Allakia", "participants": [2, 1]},
        )
        second = record_history_event(
            session,
            event_key="battle:0:test",
            world_tick=0,
            event_kind="battle",
            location_id=history_db["center"],
            payload={"participants": [2, 1], "winner": "Allakia"},
        )
        session.commit()
        assert first is second
        assert session.query(WorldHistoryEvent).count() == 1
        assert first.payload_json == '{"participants":[2,1],"winner":"Allakia"}'
    finally:
        session.close()


def test_export_rejects_finalized_gaps(history_db):
    session = create_session()
    try:
        session.add_all(
            [
                WorldSnapshot(world_tick=0, day=1, watch=1, schema_version=1, state_json="{}", is_final=True, captured_at=datetime.now(timezone.utc)),
                WorldSnapshot(world_tick=2, day=1, watch=3, schema_version=1, state_json="{}", is_final=True, captured_at=datetime.now(timezone.utc)),
            ]
        )
        session.commit()
        with pytest.raises(ValueError, match="missing: \\[1\\]"):
            select_snapshots(session, start_tick=None, end_tick=None, include_provisional=False)
    finally:
        session.close()


def test_event_markers_persist_for_three_available_frames():
    snapshots = [WorldSnapshot(world_tick=tick) for tick in range(5)]
    event = {"world_tick": 1, "kind": "battle", "payload": {}}
    scheduled = schedule_events_for_frames(snapshots, [event], duration=3)
    assert sorted(scheduled) == [1, 2, 3]
    assert all(rows == [event] for rows in scheduled.values())


def test_frames_only_export_has_dimensions_manifest_and_does_not_mutate_db(history_db):
    Image = pytest.importorskip("PIL.Image")
    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        capture_world_snapshot(session, clock, is_final=True)
        record_history_event(
            session,
            event_key="conquest:0:test",
            world_tick=0,
            event_kind="stronghold_conquest",
            location_id=history_db["center"],
            payload={"location_h3": history_db["center"], "new_controller": "Allakia"},
        )
        session.commit()
        before = (session.query(WorldSnapshot).count(), session.query(WorldHistoryEvent).count())
    finally:
        session.close()
    output = history_db["tmp_path"] / "export"
    manifest = export_history(output_dir=output, width=640, height=360, no_video=True, config_path=DEFAULT_CONFIG)
    with Image.open(output / "frames" / "frame_000000.png") as frame:
        assert frame.size == (640, 360)
    assert manifest["frame_count"] == 1
    assert json.loads((output / "manifest.json").read_text())["fps"] == 2.0
    session = create_session()
    try:
        assert (session.query(WorldSnapshot).count(), session.query(WorldHistoryEvent).count()) == before
    finally:
        session.close()
