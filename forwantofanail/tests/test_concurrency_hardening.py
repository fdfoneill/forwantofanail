from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fractions import Fraction
import json

import pytest
from fastapi import HTTPException
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from forwantofanail.api import routes
from forwantofanail.api.schemas import (
    ActionCreateRequest,
    ActionPlanRequest,
    ClaimRequest,
    ArmyManagementApplyRequest,
    ArmyManagementArmySideRequest,
    ArmyManagementRightTargetRequest,
    LoginRequest,
    MessageCreateRequest,
    StandingFollowRoadUpdateRequest,
)
from forwantofanail.core.database import Base, create_session, get_engine, reset_database_runtime
from forwantofanail.core.initialize_db import _drop_all_tables_for_reset
from forwantofanail.core.migrate_runtime_tables import migrate_runtime_tables
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
    Location,
    Message,
    Siege,
    SiegeParticipant,
    StandingOrder,
    Stronghold,
    TerrainType,
    WorldHistoryEvent,
)


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    reset_database_runtime()
    migrate_runtime_tables()
    session = create_session()
    try:
        session.add(TerrainType(terrain_id=1, terrain_name="Plains", speed_multiplier=1.0, scout_multiplier=1.0))
        session.add_all(
            [
                Location(location_id="origin_1", terrain_id=1, is_road=True, settlement=1),
                Location(location_id="origin_2", terrain_id=1, is_road=True, settlement=1),
                Location(location_id="fort_1", terrain_id=1, is_road=True, settlement=1),
            ]
        )
        session.add_all(
            [
                Commander(commander_id=1, commander_name="Alpha", commander_age=30, commander_title="Lord"),
                Commander(commander_id=2, commander_name="Beta", commander_age=30, commander_title="Lady"),
                Commander(commander_id=3, commander_name="Gamma", commander_age=30, commander_title="Lord"),
            ]
        )
        session.add_all(
            [
                Stronghold(
                    stronghold_id=1,
                    stronghold_name="Testfort",
                    stronghold_type="town",
                    location_id="fort_1",
                    control="Beta",
                    stronghold_threshold=0,
                ),
                Army(
                    army_id=1,
                    location_id="origin_1",
                    army_name="Alpha Host",
                    army_faction="Alpha",
                    commander_id=1,
                    army_supply=100,
                    army_morale=9,
                    army_resting_morale=9,
                ),
                Army(
                    army_id=2,
                    location_id="fort_1",
                    army_name="Beta Guard",
                    army_faction="Beta",
                    commander_id=2,
                    army_supply=100,
                    army_morale=9,
                    army_resting_morale=9,
                ),
                Army(
                    army_id=3,
                    location_id="origin_2",
                    army_name="Gamma Host",
                    army_faction="Alpha",
                    commander_id=3,
                    army_supply=100,
                    army_morale=9,
                    army_resting_morale=9,
                ),
            ]
        )
        session.add_all(
            [
                Detachment(detachment_id=1, detachment_name="Alpha Spears", army_id=1, warrior_count=100),
                Detachment(detachment_id=2, detachment_name="Beta Spears", army_id=2, warrior_count=100),
                Detachment(detachment_id=3, detachment_name="Gamma Spears", army_id=3, warrior_count=100),
            ]
        )
        session.commit()
    finally:
        session.close()
    yield
    reset_database_runtime()


def _call_with_session(fn):
    session = create_session()
    try:
        return fn(session)
    except HTTPException as exc:
        return exc.status_code
    finally:
        session.close()


def test_concurrent_same_commander_actions_leave_one_in_progress(sqlite_db):
    payload = ActionCreateRequest(kind="forage")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: _call_with_session(lambda session: routes.create_action(payload, commander_id=1, session=session)),
                range(2),
            )
        )

    session = create_session()
    try:
        in_progress = (
            session.query(Action)
            .filter(Action.commander_id == 1, Action.state == "in_progress")
            .count()
        )
        assert all(not isinstance(result, int) for result in results)
        assert in_progress == 1
    finally:
        session.close()


def test_concurrent_besiege_attempts_share_one_active_siege(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_ring", lambda _location_id, _distance: ["fort_1"])
    payload = ActionCreateRequest(kind="besiege", target_stronghold_id="sh_1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda commander_id: _call_with_session(
                    lambda session: routes.create_action(payload, commander_id=commander_id, session=session)
                ),
                [1, 3],
            )
        )

    session = create_session()
    try:
        assert session.query(Siege).filter(Siege.state == "active").count() == 0
        clock = session.get(GameClock, 1)
        clock.world_tick += 1
        clock.day, watch = routes.from_world_tick(clock.world_tick)
        clock.watch = int(watch)
        routes._execute_action_tick(session, clock)
        session.flush()
        assert session.query(Siege).filter(Siege.state == "active").count() == 1
        assert session.query(SiegeParticipant).filter(SiegeParticipant.state == "active").count() == 2
        assert session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_started").count() == 1
    finally:
        session.close()


def test_competing_faction_siege_orders_resolve_by_acceptance_order(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_ring", lambda _location_id, _distance: ["fort_1"])
    session = create_session()
    try:
        session.get(Army, 3).army_faction = "Gamma"
        session.commit()
    finally:
        session.close()
    session = create_session()
    try:
        routes.create_action(
            ActionCreateRequest(kind="besiege", target_stronghold_id="sh_1"),
            commander_id=3,
            session=session,
            idempotency_key="gamma-siege-first",
        )
    finally:
        session.close()
    session = create_session()
    try:
        routes.create_action(
            ActionCreateRequest(kind="besiege", target_stronghold_id="sh_1"),
            commander_id=1,
            session=session,
            idempotency_key="alpha-siege-second",
        )
    finally:
        session.close()

    _advance_one_action_tick()
    session = create_session()
    try:
        active_participants = session.query(SiegeParticipant).filter(SiegeParticipant.state == "active").all()
        assert [participant.besieger_army_id for participant in active_participants] == [3]
        alpha_action = session.query(Action).filter(Action.commander_id == 1, Action.kind == "besiege").one()
        assert alpha_action.state == "failed"
        assert session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_started").count() == 1
    finally:
        session.close()


def _seed_active_test_siege() -> int:
    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        army = session.get(Army, 1)
        stronghold = session.get(Stronghold, 1)
        action = Action(
            commander_id=1,
            kind="besiege",
            state="in_progress",
            parameters_json=json.dumps(
                {
                    "target_stronghold_id": 1,
                    "target_h3": "fort_1",
                    "target_stronghold_name": "Testfort",
                }
            ),
            accepted_at=datetime.now(timezone.utc),
            started_day=clock.day,
            started_watch=clock.watch,
        )
        session.add(action)
        session.flush()
        siege = routes._start_siege(
            session,
            army=army,
            commander_id=1,
            stronghold=stronghold,
            clock=clock,
            action=action,
        )
        siege.matin_ticks_elapsed = 3
        siege.current_resistance = 7.25
        session.commit()
        return int(siege.siege_id)
    finally:
        session.close()


def _advance_one_action_tick() -> None:
    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        clock.world_tick += 1
        clock.day, watch = routes.from_world_tick(clock.world_tick)
        clock.watch = int(watch)
        routes._execute_action_tick(session, clock)
        session.commit()
    finally:
        session.close()


def test_besieger_can_forage_without_lifting_siege(sqlite_db, monkeypatch):
    siege_id = _seed_active_test_siege()
    monkeypatch.setattr(routes.h3, "grid_disk", lambda _location_id, _radius: ["origin_1", "origin_2", "fort_1"])

    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        clock.watch = int(routes.Watch.MATIN)
        clock.world_tick = routes.to_world_tick(clock.day, clock.watch)
        army = session.get(Army, 1)
        created, cancelled_count, cancelled_by_kind = routes._apply_plan(
            session,
            commander_id=1,
            army=army,
            clock=clock,
            kind="forage",
            path=[],
            now=datetime.now(timezone.utc),
        )
        session.flush()

        assert len(created) == 1
        forage = created[0]
        assert forage.state == "queued"
        assert cancelled_count == 0
        assert cancelled_by_kind == {}
        assert session.get(Siege, siege_id).state == "active"
        assert (
            session.query(SiegeParticipant)
            .filter(
                SiegeParticipant.siege_id == siege_id,
                SiegeParticipant.besieger_army_id == 1,
                SiegeParticipant.state == "active",
            )
            .count()
            == 1
        )

        clock.world_tick += 1
        clock.day, next_watch = routes.from_world_tick(clock.world_tick)
        clock.watch = int(next_watch)
        start_result = routes._execute_action_tick(session, clock)
        session.flush()
        assert start_result["started"] == 1
        assert forage.state == "in_progress"
        assert session.get(Siege, siege_id).state == "active"

        clock.day = int(forage.eta_day)
        clock.watch = int(forage.eta_watch)
        clock.world_tick = routes.to_world_tick(clock.day, clock.watch)
        result = routes._execute_action_tick(session, clock)
        session.flush()

        assert result["completed"] == 1
        assert forage.state == "completed"
        assert session.get(Siege, siege_id).state == "active"
        restored_action = (
            session.query(Action)
            .filter(
                Action.commander_id == 1,
                Action.kind == "besiege",
                Action.state == "in_progress",
            )
            .one()
        )
        restored_parameters = json.loads(restored_action.parameters_json)
        assert restored_parameters["target_stronghold_id"] == 1
        assert restored_parameters["target_stronghold_name"] == "Testfort"
        assert (
            session.query(WorldHistoryEvent)
            .filter(WorldHistoryEvent.event_kind == "siege_ended")
            .count()
            == 0
        )
    finally:
        session.close()


def test_forage_fails_if_army_becomes_a_besieged_defender_before_completion(sqlite_db, monkeypatch):
    _seed_active_test_siege()
    monkeypatch.setattr(routes.h3, "grid_disk", lambda _location_id, _radius: ["fort_1"])

    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        defender = session.get(Army, 2)
        location = session.get(Location, "fort_1")
        original_depletion = int(location.foraged_this_season or 0)
        action = Action(
            commander_id=2,
            kind="forage",
            state="in_progress",
            parameters_json="{}",
            accepted_at=datetime.now(timezone.utc),
            started_day=clock.day,
            started_watch=clock.watch,
            eta_day=clock.day,
            eta_watch=clock.watch,
        )
        session.add(action)
        session.flush()

        result = routes._execute_action_tick(session, clock)
        session.flush()

        assert result["failed"] == 1
        assert action.state == "failed"
        assert int(location.foraged_this_season or 0) == original_depletion
        assert defender.location_id == "fort_1"
    finally:
        session.close()


def test_unbesieged_stronghold_occupant_can_sally_against_adjacent_enemy(sqlite_db, monkeypatch):
    monkeypatch.setattr(
        routes.h3,
        "grid_ring",
        lambda location_id, _distance: ["origin_1"] if location_id == "fort_1" else ["fort_1"],
    )
    monkeypatch.setattr(routes.h3, "grid_disk", lambda location_id, _radius: [location_id])

    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        clock.watch = int(routes.Watch.PRIME)
        clock.world_tick = routes.to_world_tick(clock.day, clock.watch)
        occupant = session.get(Army, 2)
        occupant.army_morale = 2
        session.get(Army, 1).army_morale = 12
        session.commit()

        targets = routes.get_valid_attack_targets(
            staged_path=None,
            commander_id=2,
            session=session,
        )
        assert [target["target_army_id"] for target in targets["targets"]] == ["army_1"]

        result = routes.create_action(
            ActionCreateRequest(kind="attack", target_h3="origin_1", target_army_id="army_1"),
            commander_id=2,
            session=session,
            idempotency_key="unbesieged-sally",
        )
        assert result["state"] == "in_progress"
    finally:
        session.close()

    session = create_session()
    try:
        action = (
            session.query(Action)
            .filter(Action.commander_id == 2, Action.kind == "attack", Action.state == "in_progress")
            .one()
        )
        clock = session.get(GameClock, 1)
        clock.day = int(action.eta_day)
        clock.watch = int(action.eta_watch)
        clock.world_tick = routes.to_world_tick(clock.day, clock.watch)
        result = routes._execute_action_tick(session, clock)
        session.flush()

        assert result["completed"] == 1
        assert action.state == "completed"
        assert session.get(Army, 2).location_id == "fort_1"
    finally:
        session.close()


def test_besieged_stronghold_still_limits_sorties_to_active_besiegers(sqlite_db, monkeypatch):
    _seed_active_test_siege()
    monkeypatch.setattr(
        routes.h3,
        "grid_ring",
        lambda location_id, _distance: ["origin_1", "origin_2"] if location_id == "fort_1" else ["fort_1"],
    )

    session = create_session()
    try:
        targets = routes.get_valid_attack_targets(
            staged_path=None,
            commander_id=2,
            session=session,
        )
        assert [target["target_army_id"] for target in targets["targets"]] == ["army_1"]

        with pytest.raises(HTTPException) as exc_info:
            routes.create_action(
                ActionCreateRequest(kind="attack", target_h3="origin_2", target_army_id="army_3"),
                commander_id=2,
                session=session,
                idempotency_key="invalid-nonbesieger-sortie",
            )
        assert exc_info.value.status_code == 400
        assert "only active besiegers" in str(exc_info.value.detail)
    finally:
        session.close()


def test_stronghold_capture_displaces_empty_field_army_without_deleting_commander(sqlite_db, monkeypatch):
    siege_id = _seed_active_test_siege()
    monkeypatch.setattr(routes, "list_valid_destinations", lambda _session, army_id: ["retreat_1"] if army_id == 2 else [])
    monkeypatch.setattr(
        routes,
        "_nearest_distance_to_armies",
        lambda location_id, _winner_armies: 1 if location_id == "retreat_1" else 0,
    )

    session = create_session()
    try:
        session.add(Location(location_id="retreat_1", terrain_id=1, is_road=True, settlement=1))
        defender = session.get(Army, 2)
        for detachment in list(defender.detachments):
            detachment.warrior_count = 0
            session.delete(detachment)
        session.add(
            Army(
                army_id=4,
                location_id="fort_1",
                army_name="Testfort Garrison",
                army_faction="Beta",
                commander_id=None,
                garrison_stronghold_id=1,
                army_supply=0,
                army_morale=9,
                army_resting_morale=9,
                is_garrison=True,
                noncombattant_percent=0.0,
            )
        )
        session.flush()

        clock = session.get(GameClock, 1)
        attacker = session.get(Army, 1)
        stronghold = session.get(Stronghold, 1)
        siege = session.get(Siege, siege_id)

        assert routes._clear_remaining_defenders_for_capture(
            session,
            clock=clock,
            stronghold=stronghold,
            attacker=attacker,
        )
        assert session.get(Army, 2).location_id == "retreat_1"
        assert session.get(Army, 2).commander_id == 2

        assert routes._finalize_siege_capture(
            session,
            clock=clock,
            siege=siege,
            stronghold=stronghold,
            attacker=attacker,
            apply_loot=False,
        )
        assert attacker.location_id == "fort_1"
        assert stronghold.control == "Alpha"
        assert session.get(Army, 4).army_faction == "Alpha"
        assert session.get(Commander, 2) is not None
    finally:
        session.close()


def test_stronghold_capture_does_not_delete_empty_field_army_when_displacement_is_blocked(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes, "list_valid_destinations", lambda _session, _army_id: [])

    session = create_session()
    try:
        defender = session.get(Army, 2)
        for detachment in list(defender.detachments):
            detachment.warrior_count = 0
            session.delete(detachment)
        session.flush()

        cleared = routes._clear_remaining_defenders_for_capture(
            session,
            clock=session.get(GameClock, 1),
            stronghold=session.get(Stronghold, 1),
            attacker=session.get(Army, 1),
        )

        assert cleared is False
        assert session.get(Army, 2) is not None
        assert session.get(Army, 2).location_id == "fort_1"
        assert session.get(Commander, 2) is not None
    finally:
        session.close()


def test_march_displaces_empty_hostile_field_army(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes, "list_valid_destinations", lambda _session, army_id: ["escape_1"] if army_id == 2 else [])

    session = create_session()
    try:
        session.add_all(
            [
                Location(location_id="target_1", terrain_id=1, is_road=True, settlement=1),
                Location(location_id="escape_1", terrain_id=1, is_road=True, settlement=1),
            ]
        )
        defender = session.get(Army, 2)
        defender.location_id = "target_1"
        for detachment in list(defender.detachments):
            detachment.warrior_count = 0
            session.delete(detachment)
        session.flush()

        attacker = session.get(Army, 1)
        moved = routes._execute_move_to_destination(
            session,
            session.get(GameClock, 1),
            attacker,
            "target_1",
        )

        assert moved is True
        assert attacker.location_id == "target_1"
        assert session.get(Army, 2).location_id == "escape_1"
        assert session.get(Army, 2).commander_id == 2
        assert session.get(Commander, 2) is not None
    finally:
        session.close()


def test_march_is_blocked_when_empty_hostile_field_army_cannot_be_displaced(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes, "list_valid_destinations", lambda _session, _army_id: [])

    session = create_session()
    try:
        session.add(Location(location_id="target_1", terrain_id=1, is_road=True, settlement=1))
        defender = session.get(Army, 2)
        defender.location_id = "target_1"
        for detachment in list(defender.detachments):
            detachment.warrior_count = 0
            session.delete(detachment)
        session.flush()

        attacker = session.get(Army, 1)
        origin_h3 = attacker.location_id
        moved = routes._execute_move_to_destination(
            session,
            session.get(GameClock, 1),
            attacker,
            "target_1",
        )

        assert moved is False
        assert attacker.location_id == origin_h3
        assert session.get(Army, 2).location_id == "target_1"
        assert session.get(Army, 2) is not None
        assert session.get(Commander, 2) is not None
    finally:
        session.close()


def test_same_watch_march_then_same_siege_preserves_continuity(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_ring", lambda _location_id, _distance: ["fort_1"])
    monkeypatch.setattr(routes, "calculate_move_watches", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(routes, "calculate_move_watches_from_origin", lambda *_args, **_kwargs: 1)
    siege_id = _seed_active_test_siege()

    session = create_session()
    try:
        routes.plan_actions(
            ActionPlanRequest(kind="march", path=["origin_2"]),
            commander_id=1,
            session=session,
            idempotency_key="stage-march-away",
        )
    finally:
        session.close()
    session = create_session()
    try:
        siege = session.get(Siege, siege_id)
        assert siege.state == "active"
        assert siege.matin_ticks_elapsed == 3
        assert siege.current_resistance == 7.25
    finally:
        session.close()

    session = create_session()
    try:
        result = routes.create_action(
            ActionCreateRequest(kind="besiege", target_stronghold_id="sh_1"),
            commander_id=1,
            session=session,
            idempotency_key="resume-same-siege",
        )
        assert result["state"] == "queued"
    finally:
        session.close()

    _advance_one_action_tick()
    session = create_session()
    try:
        siege = session.get(Siege, siege_id)
        assert siege.state == "active"
        assert siege.matin_ticks_elapsed == 3
        assert siege.current_resistance == 7.25
        assert session.query(Siege).count() == 1
        assert session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_started").count() == 1
        assert session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_ended").count() == 0
    finally:
        session.close()


def test_siege_ends_when_march_progresses_at_transition(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_ring", lambda _location_id, _distance: ["fort_1"])
    monkeypatch.setattr(routes, "calculate_move_watches", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(routes, "calculate_move_watches_from_origin", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(routes, "_movement_capacity_for_interval_start", lambda *_args, **_kwargs: 1)
    siege_id = _seed_active_test_siege()

    session = create_session()
    try:
        routes.plan_actions(
            ActionPlanRequest(kind="march", path=["origin_2"]),
            commander_id=1,
            session=session,
            idempotency_key="committed-march-away",
        )
    finally:
        session.close()
    session = create_session()
    try:
        assert session.get(Siege, siege_id).state == "active"
    finally:
        session.close()

    _advance_one_action_tick()
    session = create_session()
    try:
        assert session.get(Siege, siege_id).state == "lifted"
        event = session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_ended").one()
        assert event.world_tick == 1
    finally:
        session.close()


def test_blocked_march_does_not_lift_siege(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_ring", lambda _location_id, _distance: ["fort_1"])
    monkeypatch.setattr(routes, "calculate_move_watches", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(routes, "calculate_move_watches_from_origin", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(routes, "_movement_capacity_for_interval_start", lambda *_args, **_kwargs: 0)
    siege_id = _seed_active_test_siege()
    session = create_session()
    try:
        routes.plan_actions(
            ActionPlanRequest(kind="march", path=["origin_2"]),
            commander_id=1,
            session=session,
            idempotency_key="blocked-march-away",
        )
    finally:
        session.close()

    _advance_one_action_tick()
    session = create_session()
    try:
        assert session.get(Siege, siege_id).state == "active"
        assert session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_ended").count() == 0
    finally:
        session.close()


def test_new_siege_is_deferred_until_watch_execution(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_ring", lambda _location_id, _distance: ["fort_1"])
    session = create_session()
    try:
        result = routes.create_action(
            ActionCreateRequest(kind="besiege", target_stronghold_id="sh_1"),
            commander_id=1,
            session=session,
            idempotency_key="deferred-new-siege",
        )
        assert result["state"] == "queued"
    finally:
        session.close()
    session = create_session()
    try:
        assert session.query(Siege).filter(Siege.state == "active").count() == 0
        assert session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_started").count() == 0
    finally:
        session.close()

    _advance_one_action_tick()
    session = create_session()
    try:
        assert session.query(Siege).filter(Siege.state == "active").count() == 1
        event = session.query(WorldHistoryEvent).filter(WorldHistoryEvent.event_kind == "siege_started").one()
        assert event.world_tick == 1
    finally:
        session.close()


def test_concurrent_army_management_stale_baseline_gets_conflict(sqlite_db):
    baseline_session = create_session()
    try:
        state = routes.get_army_management_state(commander_id=1, session=baseline_session)
    finally:
        baseline_session.close()

    payload = ArmyManagementApplyRequest(
        baseline_hash=state["baseline"]["snapshot_hash"],
        left_army=ArmyManagementArmySideRequest(
            army_id="army_1",
            name="Alpha Host Renamed",
            commander_id="cmd_1",
            supply_current=100,
            detachment_ids=["det_1"],
        ),
        right_target=ArmyManagementRightTargetRequest(mode="none"),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: _call_with_session(lambda session: routes.apply_army_management(payload, commander_id=1, session=session)),
                range(2),
            )
        )

    assert sorted(409 if result == 409 else 200 for result in results) == [200, 409]


def _seed_garrison_management_pair() -> dict:
    session = create_session()
    try:
        left_army = session.get(Army, 1)
        left_army.location_id = "fort_1"
        left_army.army_supply = 1000
        session.get(Stronghold, 1).control = "Alpha"
        session.add(Detachment(detachment_id=4, detachment_name="Alpha Rearguard", army_id=1, warrior_count=10))
        session.add(
            Army(
                army_id=4,
                location_id="fort_1",
                army_name="Testfort Garrison",
                army_faction="Alpha",
                commander_id=None,
                garrison_stronghold_id=1,
                army_supply=0,
                army_morale=9,
                army_resting_morale=9,
                is_garrison=True,
                noncombattant_percent=0.0,
            )
        )
        session.add(Detachment(detachment_id=5, detachment_name="Garrison Watch", army_id=4, warrior_count=20))
        session.commit()
        return routes.get_army_management_state(commander_id=1, session=session)
    finally:
        session.close()


def _garrison_management_payload(state: dict, *, left_detachments: list[str], right_detachments: list[str], left_supply: int, right_supply=None):
    return ArmyManagementApplyRequest(
        baseline_hash=state["baseline"]["snapshot_hash"],
        left_army=ArmyManagementArmySideRequest(
            army_id="army_1",
            name="Alpha Host",
            commander_id="cmd_1",
            supply_current=left_supply,
            detachment_ids=left_detachments,
        ),
        right_target=ArmyManagementRightTargetRequest(mode="existing", army_id="army_4"),
        right_army=ArmyManagementArmySideRequest(
            army_id="army_4",
            name="Testfort Garrison",
            commander_id=None,
            supply_current=right_supply,
            detachment_ids=right_detachments,
        ),
    )


def test_field_detachment_can_join_garrison_with_confirmed_capacity_loss(sqlite_db):
    state = _seed_garrison_management_pair()
    payload = _garrison_management_payload(
        state,
        left_detachments=["det_4"],
        right_detachments=["det_1", "det_5"],
        left_supply=180,
    )

    session = create_session()
    try:
        routes.apply_army_management(
            payload,
            commander_id=1,
            session=session,
            idempotency_key="garrison-capacity-loss",
        )
    finally:
        session.close()

    session = create_session()
    try:
        assert session.get(Army, 1).army_supply == 180
        assert session.get(Army, 4).army_supply == 0
        assert session.get(Detachment, 1).army_id == 4
        assert session.get(Detachment, 4).army_id == 1
    finally:
        session.close()


def test_garrison_detachment_can_join_field_army_without_supply_change(sqlite_db):
    state = _seed_garrison_management_pair()
    payload = _garrison_management_payload(
        state,
        left_detachments=["det_1", "det_4", "det_5"],
        right_detachments=[],
        left_supply=1000,
    )

    session = create_session()
    try:
        routes.apply_army_management(
            payload,
            commander_id=1,
            session=session,
            idempotency_key="pull-from-garrison",
        )
    finally:
        session.close()

    session = create_session()
    try:
        assert session.get(Army, 1).army_supply == 1000
        assert session.get(Army, 4).army_supply == 0
        assert session.get(Detachment, 5).army_id == 1
    finally:
        session.close()


@pytest.mark.parametrize(
    ("left_supply", "right_supply"),
    [
        (179, None),
        (1000, None),
        (180, 1),
    ],
)
def test_garrison_management_rejects_arbitrary_supply_changes(sqlite_db, left_supply, right_supply):
    state = _seed_garrison_management_pair()
    payload = _garrison_management_payload(
        state,
        left_detachments=["det_4"],
        right_detachments=["det_1", "det_5"],
        left_supply=left_supply,
        right_supply=right_supply,
    )

    session = create_session()
    try:
        with pytest.raises(HTTPException) as exc_info:
            routes.apply_army_management(
                payload,
                commander_id=1,
                session=session,
                idempotency_key=f"reject-garrison-supply-{left_supply}-{right_supply}",
            )
        assert exc_info.value.status_code == 400
    finally:
        session.close()


def test_get_standing_orders_does_not_insert_row(sqlite_db):
    session = create_session()
    try:
        assert routes.get_my_standing_orders(commander_id=1, session=session)["follow_road"]["enabled"] is False
        assert session.query(StandingOrder).count() == 0
    finally:
        session.close()


def test_message_read_and_standing_order_writes_are_guarded(sqlite_db):
    seed = create_session()
    try:
        seed.add(
            Message(
                message_id=1,
                sender_name="Courier",
                recipient_id=1,
                content="Report",
                priority="normal",
                sent_day=1,
                sent_watch=1,
                delivery_day=1,
                delivery_watch=1,
                status="received",
                is_read=False,
                created_at=datetime.now(timezone.utc),
            )
        )
        seed.commit()
    finally:
        seed.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda call: _call_with_session(call),
                [
                    lambda session: routes.get_message("msg_1", commander_id=1, session=session),
                    lambda session: routes.get_message("msg_1", commander_id=1, session=session),
                    lambda session: routes.set_follow_road_standing_order(
                        StandingFollowRoadUpdateRequest(enabled=True),
                        commander_id=1,
                        session=session,
                    ),
                    lambda session: routes.set_follow_road_standing_order(
                        StandingFollowRoadUpdateRequest(enabled=False),
                        commander_id=1,
                        session=session,
                    ),
                ],
            )
        )

    assert all(not isinstance(result, int) for result in results)


def test_runtime_migration_indexes_are_idempotent(sqlite_db):
    migrate_runtime_tables()
    migrate_runtime_tables()

    session = create_session()
    try:
        index_names = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            ).all()
        }
        assert "uq_actions_one_in_progress_per_commander" in index_names
        assert "uq_sieges_one_active_per_stronghold" in index_names
        assert "uq_siege_participants_one_active_per_army" in index_names
        commander_columns = {
            row[1]
            for row in session.execute(text("PRAGMA table_info(commanders)")).all()
        }
        assert {"created_by_commander_id", "created_day", "created_watch"}.issubset(commander_columns)
    finally:
        session.close()


def test_commander_claim_issues_scoped_token_and_blocks_duplicates(sqlite_db):
    first = _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))
    second = _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))

    assert first["commander"]["id"] == "cmd_1"
    assert first["commander"]["claimed"] is True
    assert first["commander"]["faction"] == "Alpha"
    assert second == 409

    session = create_session()
    try:
        assert routes._get_current_commander_id(authorization=f"Bearer {first['token']}", session=session) == 1
        assert session.query(CommanderClaim).filter(CommanderClaim.commander_id == 1).count() == 1
        assert session.query(AuthToken).filter(AuthToken.commander_id == 1).count() == 1
    finally:
        session.close()


def test_commander_claim_list_marks_claimed_commanders(sqlite_db):
    _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))

    session = create_session()
    try:
        claims = routes.list_commander_claims(session=session)
        by_id = {row["id"]: row for row in claims}
        assert by_id["cmd_1"]["claimed"] is True
        assert by_id["cmd_1"]["faction"] == "Alpha"
        assert by_id["cmd_1"]["is_original"] is True
        assert by_id["cmd_2"]["claimed"] is False
        assert by_id["cmd_2"]["faction"] == "Beta"
    finally:
        session.close()


def test_commander_claim_overview_combines_faction_and_commander(sqlite_db, tmp_path, monkeypatch):
    faction_path = tmp_path / "faction_overviews.json"
    commander_path = tmp_path / "commander_overviews.json"
    faction_path.write_text(json.dumps({"Alpha": "Alpha faction overview."}), encoding="utf-8")
    commander_path.write_text(json.dumps({"cmd_1": "Alpha commander overview."}), encoding="utf-8")
    monkeypatch.setattr(routes, "FACTION_OVERVIEWS_PATH", faction_path)
    monkeypatch.setattr(routes, "COMMANDER_OVERVIEWS_PATH", commander_path)

    session = create_session()
    try:
        claims = routes.list_commander_claims(session=session)
        by_id = {row["id"]: row for row in claims}
        assert by_id["cmd_1"]["overview"]["faction"] == "Alpha faction overview."
        assert by_id["cmd_1"]["overview"]["commander"] == "Alpha commander overview."
        assert by_id["cmd_1"]["overview"]["combined"] == "Alpha commander overview.\n\nAlpha faction overview."
    finally:
        session.close()


def test_generated_commander_claim_overview_uses_dispatch_formula(sqlite_db):
    session = create_session()
    try:
        session.add(Commander(commander_id=4, commander_name="Delta", commander_age=30, commander_title="Captain", created_by_commander_id=1, created_day=4, created_watch=2))
        session.add(
            Army(
                army_id=4,
                location_id="origin_2",
                army_name="Delta Host",
                army_faction="Alpha",
                commander_id=4,
                army_supply=100,
                army_morale=9,
                army_resting_morale=9,
            )
        )
        session.add(Detachment(detachment_id=4, detachment_name="Delta Spears", army_id=4, warrior_count=100))
        session.commit()

        claims = routes.list_commander_claims(session=session)
        by_id = {row["id"]: row for row in claims}
        expected_date = routes._scenario_date_for_day(4).isoformat()
        assert by_id["cmd_4"]["overview"]["commander"] == f"Dispatched by Lord Alpha on {expected_date}."
        assert by_id["cmd_4"]["is_original"] is False
    finally:
        session.close()


def test_runtime_commander_portrait_falls_back_to_faction_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "COMMANDER_PORTRAIT_DIR", tmp_path)
    commander = Commander(commander_name="Delta", commander_title="Captain", commander_age=30)
    faction_portrait = tmp_path / "portrait_boonan.png"
    faction_portrait.write_bytes(b"faction portrait")

    assert routes._commander_portrait_filename(commander, "Boonan") == "portrait_boonan.png"
    assert routes._commander_portrait_url(commander, "Boonan") == "/v1/commander-portraits/portrait_boonan.png"


def test_named_commander_portrait_takes_precedence_over_faction_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "COMMANDER_PORTRAIT_DIR", tmp_path)
    commander = Commander(commander_name="Delta", commander_title="Captain", commander_age=30)
    (tmp_path / "Portrait - Captain Delta.png").write_bytes(b"named portrait")
    (tmp_path / "portrait_boonan.png").write_bytes(b"faction portrait")

    assert routes._commander_portrait_filename(commander, "Boonan") == "Portrait - Captain Delta.png"


def test_runtime_commander_without_portrait_asset_keeps_empty_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "COMMANDER_PORTRAIT_DIR", tmp_path)
    commander = Commander(commander_name="Delta", commander_title="Captain", commander_age=30)

    assert routes._commander_portrait_filename(commander, "Boonan") is None
    assert routes._commander_portrait_url(commander, "Boonan") is None


def test_direct_login_claims_unclaimed_commander_and_blocks_duplicates(sqlite_db):
    first = _call_with_session(
        lambda session: routes.login(LoginRequest(commander_name="Alpha"), session=session, x_admin_token=None)
    )
    second = _call_with_session(
        lambda session: routes.login(LoginRequest(commander_name="Alpha"), session=session, x_admin_token=None)
    )

    assert first["commander"]["name"] == "Alpha"
    assert second == 409


def test_direct_admin_login_bypasses_claims_when_configured(sqlite_db, monkeypatch):
    monkeypatch.setenv("DEV_ADMIN_TOKEN", "secret")
    _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))

    allowed = _call_with_session(
        lambda session: routes.login(LoginRequest(commander_name="Alpha"), session=session, x_admin_token="secret")
    )

    assert allowed["commander"]["name"] == "Alpha"


def test_admin_claim_reset_releases_commanders(sqlite_db, monkeypatch):
    monkeypatch.setenv("DEV_ADMIN_TOKEN", "secret")
    _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))

    reset = _call_with_session(lambda session: routes.reset_commander_claims(session=session, x_admin_token="secret"))

    assert reset == {"reset_claims": 1}
    session = create_session()
    try:
        assert session.query(CommanderClaim).count() == 0
        assert session.query(AuthToken).count() == 0
    finally:
        session.close()


def test_admin_army_summary_reports_commander_armies_with_diegetic_locations(sqlite_db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "summary-secret")
    monkeypatch.setattr(
        routes,
        "describe_army_location",
        lambda _session, location_h3: f"near {location_h3}",
    )
    session = create_session()
    try:
        rows = routes.admin_armies_summary(session=session, x_admin_token="summary-secret")
        by_army = {row["army_name"]: row for row in rows}
        assert by_army["Alpha Host"]["commander_name"] == "Lord Alpha"
        assert by_army["Alpha Host"]["faction"] == "Alpha"
        assert by_army["Alpha Host"]["claimed"] is False
        assert by_army["Alpha Host"]["location"] == "near origin_1"
        assert "strength" not in by_army["Alpha Host"]
        assert by_army["Alpha Host"]["status"] == "idle"
    finally:
        session.close()


def test_sqlite_reset_drop_handles_claim_foreign_keys(sqlite_db):
    _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))

    engine = get_engine()
    _drop_all_tables_for_reset(engine)

    with engine.connect() as connection:
        remaining_tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            ).all()
        }
    assert remaining_tables == set()


def test_world_tick_orders_night_after_vesper():
    from forwantofanail.mechanics.time import Watch, from_world_tick, to_world_tick

    assert to_world_tick(7, Watch.VESPER) + 1 == to_world_tick(7, Watch.NIGHT)
    assert from_world_tick(to_world_tick(7, Watch.NIGHT) + 1) == (8, Watch.MATIN)


@pytest.mark.parametrize(
    ("depletion", "expected_yield"),
    [
        (0, Fraction(2500, 1)),
        (1, Fraction(2500, 3)),
        (2, Fraction(2500, 9)),
        (3, Fraction(0, 1)),
    ],
)
def test_forage_yield_scales_with_pre_forage_depletion(depletion, expected_yield):
    location = Location(settlement=1, foraged_this_season=depletion)

    assert routes._forage_yield_for_location(location) == expected_yield


@pytest.mark.parametrize(
    ("average_depletion", "expected_word"),
    [
        (0, "untouched"),
        (0.5, "plentiful"),
        (1, "picked-over"),
        (1.99, "picked-over"),
        (2, "exhausted"),
        (3, "exhausted"),
    ],
)
def test_forage_condition_word_uses_pre_forage_average(average_depletion, expected_word):
    assert routes._forage_condition_word(average_depletion) == expected_word


def test_completed_forage_increments_cells_and_reports_prior_condition(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_disk", lambda _location_id, _radius: ["origin_1", "origin_2", "fort_1"])
    session = create_session()
    try:
        locations = {
            location.location_id: location
            for location in session.query(Location)
            .filter(Location.location_id.in_(["origin_1", "origin_2", "fort_1"]))
            .all()
        }
        locations["origin_1"].foraged_this_season = 0
        locations["origin_2"].foraged_this_season = 1
        locations["fort_1"].foraged_this_season = 2
        army = session.get(Army, 1)
        gain, forageable_locations, average_depletion = routes._forage_supply_gain_for_army(session, army)
        assert gain == 3333
        assert {location.location_id for location in forageable_locations} == {"origin_1", "origin_2"}
        assert average_depletion == 0.5

        clock = session.get(GameClock, 1)
        session.add(
            Action(
                commander_id=1,
                kind="forage",
                state="in_progress",
                parameters_json="{}",
                accepted_at=datetime.now(timezone.utc),
                started_day=clock.day,
                started_watch=clock.watch,
                eta_day=clock.day,
                eta_watch=clock.watch,
            )
        )
        session.flush()
        result = routes._execute_action_tick(session, clock)
        session.flush()

        assert result["completed"] == 1
        assert [
            locations[location_id].foraged_this_season
            for location_id in ("origin_1", "origin_2", "fort_1")
        ] == [1, 2, 2]
        alert = session.query(Alert).filter(Alert.recipient_commander_id == 1).order_by(Alert.alert_id.desc()).first()
        assert alert is not None
        assert "foraged from plentiful country" in alert.message
    finally:
        session.close()


def test_forage_excludes_enemy_field_armies_and_garrisons(sqlite_db, monkeypatch):
    monkeypatch.setattr(
        routes.h3,
        "grid_disk",
        lambda _location_id, _radius: ["origin_1", "origin_2", "enemy_field", "enemy_garrison"],
    )
    session = create_session()
    try:
        session.add_all(
            [
                Location(location_id="enemy_field", terrain_id=1, settlement=1),
                Location(location_id="enemy_garrison", terrain_id=1, settlement=1),
            ]
        )
        session.get(Army, 2).location_id = "enemy_field"
        session.add(
            Army(
                army_id=4,
                location_id="enemy_garrison",
                army_name="Enemy Garrison",
                army_faction="Beta",
                commander_id=None,
                garrison_stronghold_id=1,
                army_supply=0,
                army_morale=9,
                army_resting_morale=9,
                is_garrison=True,
                noncombattant_percent=0.0,
            )
        )
        session.flush()

        gain, forageable_locations, average_depletion = routes._forage_supply_gain_for_army(
            session,
            session.get(Army, 1),
        )

        assert gain == 5000
        assert {location.location_id for location in forageable_locations} == {"origin_1", "origin_2"}
        assert average_depletion == 0
    finally:
        session.close()


def test_forage_depletion_database_constraint_rejects_out_of_range_values(sqlite_db):
    session = create_session()
    try:
        location = session.get(Location, "origin_1")
        location.foraged_this_season = 4
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_night_message_is_not_visible_at_vesper(sqlite_db):
    session = create_session()
    try:
        clock = session.get(GameClock, 1)
        clock.day = 1
        clock.watch = 4
        clock.world_tick = 3
        session.add(
            Message(
                sender_name="Courier",
                recipient_id=1,
                content="Night report",
                priority="normal",
                sent_day=1,
                sent_watch=3,
                sent_tick=2,
                delivery_day=1,
                delivery_watch=0,
                delivery_tick=4,
                status="received",
                is_read=False,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        assert routes.list_messages(commander_id=1, session=session) == []
        clock.world_tick = 4
        clock.watch = 0
        session.commit()
        assert len(routes.list_messages(commander_id=1, session=session)) == 1
    finally:
        session.close()


def test_sent_letters_are_immediate_and_hide_delivery_information(sqlite_db):
    session = create_session()
    try:
        session.add(
            Message(
                sender_commander_id=1,
                sender_name="Lord Alpha",
                recipient_id=2,
                content="Advance at dawn.",
                priority="normal",
                sent_day=1,
                sent_watch=1,
                sent_tick=0,
                delivery_day=4,
                delivery_watch=3,
                delivery_tick=17,
                status="in_transit",
                is_read=False,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

        sent_list = routes.list_messages(unread_only=False, commander_id=1, session=session)
        assert len(sent_list) == 1
        assert sent_list[0]["direction"] == "sent"
        assert sent_list[0]["to"]["name"] == "Lady Beta"
        assert "delivered_watch" not in sent_list[0]
        assert "status" not in sent_list[0]
        assert routes.list_messages(unread_only=True, commander_id=1, session=session) == []

        sent_detail = routes.get_message(sent_list[0]["id"], commander_id=1, session=session)
        assert sent_detail["direction"] == "sent"
        assert sent_detail["content"] == "Advance at dawn."
        assert "delivered_watch" not in sent_detail
        assert "status" not in sent_detail

        assert routes.list_messages(unread_only=False, commander_id=2, session=session) == []
    finally:
        session.close()


def test_alert_delivery_and_read_receipt_are_per_commander(sqlite_db):
    session = create_session()
    try:
        routes._create_alert(
            session,
            recipient_commander_id=None,
            alert_type="world event",
            message="Test news",
            created_day=1,
            created_watch=1,
        )
        session.commit()
        page = routes.list_alerts(
            limit=50, unread_only=False, after_id=None, before_id=None, commander_id=1, session=session
        )
        assert len(page["items"]) == 1
        alert_id = page["items"][0]["id"]
        routes.acknowledge_alert_delivery(
            routes.AlertIdsRequest(alert_ids=[alert_id]), commander_id=1, session=session
        )
        routes.mark_alert_read(alert_id, commander_id=1, session=session)
        first = session.get(AlertRecipient, (1, 1))
        second = session.get(AlertRecipient, (1, 2))
        assert first.delivered_at is not None and first.read_at is not None
        assert second.delivered_at is None and second.read_at is None
    finally:
        session.close()


def test_hardened_claim_hashes_api_token(sqlite_db, monkeypatch):
    monkeypatch.setenv("GAME_PASSWORD", "shared-secret")
    session = create_session()
    try:
        result = routes.claim_session(
            ClaimRequest(commander_id="cmd_1", game_password="shared-secret", client_kind="api"),
            Response(),
            session=session,
            idempotency_key="claim-1",
        )
        repeated = routes.claim_session(
            ClaimRequest(commander_id="cmd_1", game_password="shared-secret", client_kind="api"),
            Response(),
            session=session,
            idempotency_key="claim-1",
        )
        assert result["token"]
        assert repeated["token"] == result["token"]
        assert session.get(AuthToken, result["token"]) is None
        assert routes._get_current_commander_id(
            authorization=f"Bearer {result['token']}", session_cookie=None, session=session
        ) == 1
    finally:
        session.close()


def test_message_send_idempotency_prevents_duplicate(sqlite_db):
    payload = MessageCreateRequest(recipient_id="cmd_2", content="One letter")
    first = _call_with_session(
        lambda session: routes.send_message(
            payload, commander_id=1, session=session, idempotency_key="letter-1"
        )
    )
    second = _call_with_session(
        lambda session: routes.send_message(
            payload, commander_id=1, session=session, idempotency_key="letter-1"
        )
    )
    assert first == second
    assert "estimated_delivery_watch" not in first
    session = create_session()
    try:
        assert session.query(Message).count() == 1
    finally:
        session.close()


def test_browser_claim_uses_httponly_cookie(sqlite_db, monkeypatch):
    monkeypatch.setenv("GAME_PASSWORD", "shared-secret")
    monkeypatch.setenv("SESSION_SECRET", "session-secret")
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.post(
            "/v1/auth/claim",
            headers={"Idempotency-Key": "browser-claim-1"},
            json={"commander_id": "cmd_1", "game_password": "shared-secret", "client_kind": "browser"},
        )
        assert response.status_code == 200
        assert "token" not in response.json()
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/v1/auth/session").status_code == 200


def _brief_endpoint_environs():
    center_h3 = "871ec9020ffffff"
    return {
        "center_h3": center_h3,
        "radius": 2,
        "cells": [
            {
                "h3": center_h3,
                "terrain_type": "Open Ground",
                "has_road": False,
                "settlement": 1,
                "foraged_this_season": 0,
                "stronghold": None,
                "other_armies": [],
            }
        ],
    }


def test_brief_endpoint_requires_auth_and_returns_plain_text_for_bearer(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes, "_serialize_environs", lambda *_args, **_kwargs: _brief_endpoint_environs())
    monkeypatch.setattr(routes, "_border_road_neighbor_ids", lambda *_args, **_kwargs: [])
    claim = _call_with_session(lambda session: routes.claim_commander("cmd_1", session=session))
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        assert client.get("/v1/me/brief").status_code == 401
        response = client.get(
            "/v1/me/brief",
            headers={"Authorization": f"Bearer {claim['token']}"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; charset=utf-8")
    assert response.text.startswith("ARMY\nUnder your command is the Alpha Host, an army 100 strong")
    assert response.text.endswith(
        "LOCAL SITUATION\nThe army is in open ground terrain. The area is untouched in terms of forage. "
        "No other armies are nearby."
    )


def test_brief_endpoint_accepts_browser_cookie(sqlite_db, monkeypatch):
    monkeypatch.setenv("GAME_PASSWORD", "shared-secret")
    monkeypatch.setenv("SESSION_SECRET", "session-secret")
    monkeypatch.setattr(routes, "_serialize_environs", lambda *_args, **_kwargs: _brief_endpoint_environs())
    monkeypatch.setattr(routes, "_border_road_neighbor_ids", lambda *_args, **_kwargs: [])
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        claim = client.post(
            "/v1/auth/claim",
            headers={"Idempotency-Key": "brief-browser-claim"},
            json={"commander_id": "cmd_1", "game_password": "shared-secret", "client_kind": "browser"},
        )
        response = client.get("/v1/me/brief")

    assert claim.status_code == 200
    assert response.status_code == 200
    assert response.text.startswith("ARMY\nUnder your command is the Alpha Host")
    assert "The army is in open ground terrain." in response.text


def test_brief_attention_counts_do_not_mark_letters_or_alerts_read(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes, "_serialize_environs", lambda *_args, **_kwargs: _brief_endpoint_environs())
    monkeypatch.setattr(routes, "_border_road_neighbor_ids", lambda *_args, **_kwargs: [])
    session = create_session()
    try:
        clock = routes._get_or_create_clock(session)
        message = Message(
            sender_commander_id=2,
            sender_name="Lady Beta",
            recipient_id=1,
            content="Hold your ground.",
            priority="normal",
            sent_day=clock.day,
            sent_watch=clock.watch,
            sent_tick=clock.world_tick,
            delivery_day=clock.day,
            delivery_watch=clock.watch,
            delivery_tick=clock.world_tick,
            status="received",
            is_read=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(message)
        alert = routes._create_alert(
            session,
            recipient_commander_id=1,
            alert_type="report",
            signal_kind="event",
            category="orders",
            importance="high",
            message="Scouts report movement.",
            created_day=clock.day,
            created_watch=clock.watch,
        )
        session.commit()
        message_id = message.message_id
        alert_id = alert.alert_id

        brief = routes._commander_brief_text(session, 1)
        session.expire_all()

        assert "ATTENTION\nYou have 1 unread letter and 1 unread alert." in brief
        assert "1 alert is of high importance." in brief
        assert session.get(Message, message_id).is_read is False
        receipt = session.get(AlertRecipient, (alert_id, 1))
        assert receipt.read_at is None
        assert receipt.delivered_at is None
    finally:
        session.close()


def test_brief_renders_action_target_and_eta_without_internal_ids(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes, "_serialize_environs", lambda *_args, **_kwargs: _brief_endpoint_environs())
    monkeypatch.setattr(routes, "_border_road_neighbor_ids", lambda *_args, **_kwargs: [])
    session = create_session()
    try:
        action = Action(
            commander_id=1,
            kind="move",
            state="queued",
            parameters_json=json.dumps({"destination_h3": "origin_2"}),
            accepted_at=datetime.now(timezone.utc),
            eta_day=2,
            eta_watch=2,
        )
        session.add(action)
        session.commit()

        brief = routes._commander_brief_text(session, 1)

        assert "ORDERS\nThe army is currently holding." in brief
        assert "Its next ordered stage will lead toward an undescribed destination." in brief
        assert "The present stage is expected during the prime watch on May 22, 1410." in brief
        assert "1 stage remains in the ordered route." in brief
        assert "origin_2" not in brief
        assert "destination_h3" not in brief
        assert "action_" not in brief
    finally:
        session.close()


def test_brief_endpoint_uses_normal_and_cavalry_environs_radii(sqlite_db, monkeypatch):
    radii = []

    def serialize(_session, _center_h3, radius, **_kwargs):
        radii.append(radius)
        return _brief_endpoint_environs()

    monkeypatch.setattr(routes, "_serialize_environs", serialize)
    monkeypatch.setattr(routes, "_border_road_neighbor_ids", lambda *_args, **_kwargs: [])
    session = create_session()
    try:
        routes.get_my_brief(commander_id=1, session=session)
        session.get(Detachment, 1).is_cavalry = True
        session.flush()
        routes.get_my_brief(commander_id=1, session=session)
    finally:
        session.close()

    assert radii == [2, 4]


def test_admin_commander_brief_requires_admin_token(sqlite_db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "brief-admin")
    monkeypatch.setattr(routes, "_serialize_environs", lambda *_args, **_kwargs: _brief_endpoint_environs())
    monkeypatch.setattr(routes, "_border_road_neighbor_ids", lambda *_args, **_kwargs: [])
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        unauthorized = client.get("/v1/admin/commanders/cmd_2/brief")
        response = client.get(
            "/v1/admin/commanders/cmd_2/brief",
            headers={"X-Admin-Token": "brief-admin"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; charset=utf-8")
    assert response.text.startswith("ARMY\nUnder your command is the Beta Guard")
    assert "The army is in open ground terrain." in response.text


def test_dev_dashboard_opens_commander_briefs_in_text_safe_modal(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/dev/dashboard")

    assert response.status_code == 200
    assert 'id="briefModalOverlay"' in response.text
    assert 'id="briefModalText"' in response.text
    assert 'className = "summary-button"' in response.text
    assert "/v1/admin/commanders/${encodeURIComponent(commanderId)}/brief" in response.text
    assert 'els.briefModalText.textContent = String(brief' in response.text
    assert "els.summaryList.replaceChildren();" in response.text
    assert "els.summaryList.innerHTML" not in response.text
    assert 'const location = String(row.location || "at an unknown location");' in response.text
    assert "row.strength" not in response.text


def test_player_dashboard_csp_allows_h3_script_host(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert "https://cdn.jsdelivr.net" in response.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net/npm/h3-js@4.1.0/dist/h3-js.umd.js" in response.text


def test_player_dashboard_serializes_staged_path_h3_values(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert ".map(stagedStepH3)" in response.text
    assert "state.stagedPath.join" not in response.text
    assert "Order destination lookup failed" in response.text


def test_player_dashboard_release_is_only_in_commander_modal(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert response.text.count('id="logoutBtn"') == 1
    assert 'id="commanderModalOverlay"' in response.text
    assert 'id="commanderModalOverview"' in response.text
    assert response.text.index('id="commanderModalOverlay"') < response.text.index('id="logoutBtn"')


def test_player_dashboard_letters_use_modals_and_direction_labels(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert ">Letters</div>" in response.text
    assert "Received Letters" not in response.text
    assert 'id="composeModalOverlay"' in response.text
    assert 'id="letterDetailModalOverlay"' in response.text
    assert 'id="messageRead"' not in response.text
    assert 'const directionLabel = isSent ? "TO: " : "FROM: ";' in response.text
    assert 'isSent ? "Sent Letter" : "Received Letter"' in response.text
    assert "els.letterDetailDeliveredLabel.hidden = isSent" in response.text


def test_player_dashboard_labels_original_and_created_commander_roles(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert 'state.selectedCommanderIsOriginal ? "High Commander" : "Subcommander"' in response.text
    assert "`${state.selectedCommanderFaction} ${role}`" in response.text


def test_player_dashboard_merges_alert_cursors_and_only_acknowledges_rendered_rows(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert "function alertFeedRow(" in response.text
    assert "function mergeAlertEvents(" in response.text
    assert "function fetchAlertsAfter(" in response.text
    assert "after_id=${encodeURIComponent(cursor)}" in response.text
    assert "const events = mergeAlertEvents(existingEvents, incomingEvents" in response.text
    assert "const beforeId = state.alertsBeforeId" in response.text
    assert "await acknowledgeRenderedAlerts(renderedIds);" in response.text
    assert "const deliveredIds = rows.map" not in response.text


def test_player_dashboard_only_displays_cell_faction_for_strongholds(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert "cell.region_control" not in response.text
    faction_row = 'metaRows.push(`<div><span class=\\"k\\">Faction:'
    stronghold_type_row = 'metaRows.push(`<div><span class=\\"k\\">Stronghold Type:'
    conditional_index = response.text.rindex("if (hasStronghold) {", 0, response.text.index(faction_row))
    assert conditional_index < response.text.index(stronghold_type_row)
    assert conditional_index < response.text.index(faction_row)


def test_player_environs_omits_region_control_but_keeps_stronghold_faction(sqlite_db, monkeypatch):
    monkeypatch.setattr(routes.h3, "grid_disk", lambda _center, _radius: ["origin_1", "origin_2", "fort_1"])
    session = create_session()
    try:
        army = session.get(Army, 1)
        environs = routes._serialize_environs(
            session,
            army.location_id,
            radius=2,
            exclude_army_id=army.army_id,
            viewer_commander_id=1,
            viewer_army=army,
        )
        by_h3 = {cell["h3"]: cell for cell in environs["cells"]}

        assert all("region_control" not in cell for cell in environs["cells"])
        assert by_h3["fort_1"]["stronghold"]["faction"] == "Beta"
    finally:
        session.close()


def test_player_dashboard_uses_diegetic_settlement_descriptions_in_hover_cards(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    for value, description in enumerate(("Uninhabitable", "Empty", "Sparse", "Light", "Heavy", "Dense")):
        assert f'{value}: "{description}"' in response.text
    assert "const settlementText = settlementDescription(cell.settlement);" in response.text


def test_player_dashboard_uses_forage_condition_descriptions_in_hover_cards(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    for value, description in enumerate(("Untouched", "Harvested", "Picked-Over", "Exhausted")):
        assert f'{value}: "{description}"' in response.text
    assert "const forageText = forageDescription(cell.foraged_this_season);" in response.text
    assert "const canForage = Number(cell.settlement) > 0;" in response.text
    assert "if (canForage) {" in response.text
    settlement_row = '<span class=\\"k\\">Settlement:</span>${escapeHtml(settlementText)}'
    forage_row = '<span class=\\"k\\">Forage:</span>${escapeHtml(forageText)}'
    forageable_block = response.text[response.text.index("if (canForage) {"):]
    assert settlement_row in forageable_block
    assert forage_row in forageable_block
    assert "Times Foraged This Season:" not in response.text


def test_player_dashboard_auto_refreshes_every_five_seconds(sqlite_db):
    from forwantofanail.api.app import app

    with TestClient(app) as client:
        response = client.get("/player/dashboard")

    assert response.status_code == 200
    assert "const AUTO_REFRESH_INTERVAL_MS = 5000;" in response.text
    assert "}, AUTO_REFRESH_INTERVAL_MS);" in response.text
