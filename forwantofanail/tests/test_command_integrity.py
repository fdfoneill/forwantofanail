from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading

import h3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from forwantofanail.api import routes
from forwantofanail.api.app import app
from forwantofanail.api.schemas import ActionPlanRequest, ArmyManagementApplyRequest
from forwantofanail.agent_tools import services
from forwantofanail.agent_tools.registry import invoke
from forwantofanail.agent_tools.services import ToolContext
from forwantofanail.agent_runtime import worker
from forwantofanail.agent_runtime.leases import LeaseLost
from forwantofanail.agent_runtime.providers import ModelToolCall
from forwantofanail.agent_runtime.service import assign_agent, skip_run, utcnow, validate_tool_credential
from forwantofanail.core.database import create_session, get_engine
from forwantofanail.core.models import (
    Action, AgentRun, AgentRunEvent, Army, AuthToken, Commander, Detachment, GameClock,
    IdempotencyRecord, Message, Siege, SiegeParticipant, StandingOrder, WorldHistoryEvent,
)
from forwantofanail.core.sequences import synchronize_sequences


def _tick(session):
    clock = session.get(GameClock, 1)
    clock.world_tick += 1
    clock.day, watch = routes.from_world_tick(clock.world_tick)
    clock.watch = int(watch)
    result = routes._execute_action_tick(session, clock)
    session.commit()
    return result


def _siege(session, active, two_participants=False):
    state = "active" if active else "lifted"
    siege = Siege(stronghold_id=1, besieger_army_id=1, live_besieger_army_id=1,
                  besieger_commander_id=0, started_day=1, started_watch=1,
                  current_resistance=10, max_resistance=10, state=state)
    session.add(siege)
    session.flush()
    for army_id in ([1, 2] if two_participants else [1]):
        session.add(SiegeParticipant(siege_id=siege.siege_id, besieger_army_id=army_id,
                                    live_besieger_army_id=army_id, besieger_commander_id=army_id - 1,
                                    started_day=1, started_watch=1, state=state))
    session.commit()
    return siege.siege_id


@pytest.mark.parametrize("active,two,deleted_id", [(False, False, 1), (True, False, 1), (True, True, 1), (False, True, 2), (True, True, 2)])
def test_army_destruction_preserves_sieges(integrity_db, active, two, deleted_id):
    with create_session() as session:
        siege_id = _siege(session, active, two)
        session.add(Action(commander_id=deleted_id - 1, kind="move", state="queued", parameters_json="{}", accepted_at=utcnow()))
        session.add(StandingOrder(commander_id=deleted_id - 1, follow_road_enabled=True, forced_march_enabled=True, updated_at=utcnow()))
        session.commit()
        routes._destroy_army(session, session.get(Army, deleted_id), clock=session.get(GameClock, 1))
        session.commit()
        session.expire_all()
        assert session.get(Army, deleted_id) is None
        assert session.get(Commander, deleted_id - 1) is not None
        participant = session.get(SiegeParticipant, (siege_id, deleted_id))
        assert participant.besieger_army_id == deleted_id
        assert participant.live_besieger_army_id is None
        assert participant.state != "active"
        siege = session.get(Siege, siege_id)
        if active and two:
            assert siege.state == "active" and siege.live_besieger_army_id == 3 - deleted_id
        else:
            assert siege.state == "lifted"
        assert session.query(Action).filter_by(commander_id=deleted_id - 1, state="queued").count() == 0
        assert not session.get(StandingOrder, deleted_id - 1).follow_road_enabled
        assert session.query(WorldHistoryEvent).filter_by(event_kind="army_destroyed").count() == 1


def test_army_ids_are_not_reused(integrity_db):
    with create_session() as session:
        routes._destroy_army(session, session.get(Army, 3), clock=session.get(GameClock, 1))
        session.commit()
        row = Army(location_id=integrity_db["center"], army_name="Replacement", army_faction="Blue")
        session.add(row)
        session.commit()
        assert row.army_id > 3


def test_known_enemy_march_rejected_without_replacing_orders(integrity_db):
    with create_session() as session:
        session.get(Army, 3).location_id = integrity_db["neighbor"]
        session.commit()
        routes.plan_actions(ActionPlanRequest(kind="forage"), commander_id=0, session=session, idempotency_key="first")
        with pytest.raises(HTTPException, match="attack order"):
            routes.plan_actions(ActionPlanRequest(kind="march", path=[integrity_db["neighbor"]]), commander_id=0, session=session, idempotency_key="second")
        assert session.query(Action).filter_by(commander_id=0, kind="forage", state="in_progress").count() == 1


@pytest.mark.parametrize("swap", [False, True])
def test_unexpected_contact_resolves_once_and_halts(integrity_db, monkeypatch, swap):
    monkeypatch.setattr(routes.random, "randint", lambda *_: 3)
    with create_session() as session:
        # Remove friendly numerical support to make the moving-side modifiers clear.
        session.get(Army, 2).location_id = integrity_db["far"]
        destination = integrity_db["neighbor"]
        beyond = next(cell for cell in h3.grid_ring(destination, 1) if cell != integrity_db["center"] and cell != integrity_db["far"])
        # Directly seed accepted moves: the enemy was absent when orders were issued.
        session.get(Army, 3).location_id = destination
        session.add_all([
            Action(commander_id=0, kind="move", state="in_progress", parameters_json=json.dumps({"destination_h3": destination, "remaining_cost": 1}), accepted_at=utcnow()),
            Action(commander_id=0, kind="move", state="queued", parameters_json=json.dumps({"destination_h3": beyond}), accepted_at=utcnow()),
            StandingOrder(commander_id=0, follow_road_enabled=True, updated_at=utcnow()),
        ])
        if swap:
            session.add(Action(commander_id=2, kind="move", state="in_progress", parameters_json=json.dumps({"destination_h3": integrity_db["center"], "remaining_cost": 1}), accepted_at=utcnow()))
        else:
            session.add(Action(commander_id=2, kind="attack", state="in_progress", parameters_json=json.dumps({"target_h3": integrity_db["center"], "target_army_id": 1}), eta_day=1, eta_watch=2, accepted_at=utcnow()))
        session.commit()
        _tick(session)
        assert session.query(WorldHistoryEvent).filter_by(event_kind="battle").count() == 1
        assert session.get(Army, 1).location_id != session.get(Army, 3).location_id
        assert session.query(Action).filter(Action.commander_id == 0, Action.kind == "move", Action.state.in_(routes.ACTIVE_ACTION_STATES)).count() == 0
        assert not session.get(StandingOrder, 0).follow_road_enabled
        assert session.get(Army, 1).detachments[0].warrior_count < 100


def test_facade_rolls_back_when_receipt_building_fails(integrity_db, monkeypatch):
    with create_session() as session:
        ctx = ToolContext(session, 0, "binding", idempotency_key="interrupted")
        arguments = {"state_token": services._state_fingerprint(ctx), "order": {"kind": "forage"}}
        original = services._result
        def crash(*args, **kwargs):
            raise RuntimeError("injected receipt failure")
        monkeypatch.setattr(services, "_result", crash)
        with pytest.raises(RuntimeError):
            invoke("fwoan_submit_order", arguments, ctx)
        assert session.query(Action).count() == 0
        assert session.query(IdempotencyRecord).count() == 0
        monkeypatch.setattr(services, "_result", original)
        result = invoke("fwoan_submit_order", arguments, ctx)
        assert invoke("fwoan_submit_order", arguments, ctx) == result
        assert session.query(Action).count() == 1


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_concurrent_transport_retry_replays_one_receipt(integrity_db, transport):
    raw = "integrity-token"
    with create_session() as session:
        session.add(AuthToken(token=hashlib.sha256(raw.encode()).hexdigest(), commander_id=0, created_at=utcnow(), last_used_at=utcnow(), client_kind="api"))
        session.commit()
        arguments = {"state_token": services._state_fingerprint(ToolContext(session, 0, hashlib.sha256(raw.encode()).hexdigest())), "order": {"kind": "forage"}}
    barrier = threading.Barrier(2)
    def post(_):
        with TestClient(app) as client:
            barrier.wait(timeout=10)
            headers = {"Authorization": f"Bearer {raw}", "Idempotency-Key": "same"}
            if transport == "rest":
                response = client.post("/v1/tools/fwoan_submit_order", json=arguments, headers=headers)
            else:
                response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "fwoan_submit_order", "arguments": arguments}}, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(post, range(2)))
    assert first == second
    with create_session() as session:
        assert session.query(Action).count() == 1


def test_stale_worker_cannot_write_any_run_state(integrity_db):
    with create_session() as session:
        assign_agent(session, 0, "ollama_default")
        session.commit()
    old, old_token = worker.claim_run("old")
    with create_session() as session:
        session.get(AgentRun, old.run_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    new, new_token = worker.claim_run("new")
    assert new.generation > old.generation and new.run_id == old.run_id
    with create_session() as session:
        event_count = session.query(AgentRunEvent).count()
    for call in [lambda: worker.fail_run(old, "late", "old worker"),
                 lambda: worker._increment_usage(old, calls=1),
                 lambda: worker._record(old, "late", {}),
                 lambda: worker._runtime_call(old, ModelToolCall("late", "fwoan_finish_heartbeat", {}))]:
        with pytest.raises(LeaseLost):
            call()
    with create_session() as session:
        assert session.get(AgentRun, new.run_id).status == "running"
        assert session.query(AgentRunEvent).count() == event_count
        with pytest.raises(HTTPException):
            validate_tool_credential(session, old_token, 0)
        session.rollback()
        validate_tool_credential(session, new_token, 0)


def _management_request(session, commander, target, supply):
    left = routes._find_commander_army(session, commander)
    right = session.get(Army, target)
    baseline = routes._army_management_snapshot_hash(left, routes._eligible_management_armies(session, left))
    return ArmyManagementApplyRequest.model_validate({
        "baseline_hash": baseline,
        "left_army": {"army_id": f"army_{left.army_id}", "name": left.army_name, "commander_id": f"cmd_{commander}", "supply_current": supply,
                      "detachment_ids": [f"det_{det.detachment_id}" for det in left.detachments]},
        "right_target": {"mode": "existing", "army_id": f"army_{target}"},
        "right_army": {"army_id": f"army_{target}", "name": right.army_name, "commander_id": f"cmd_{right.commander_id}", "supply_current": 200 - supply,
                       "detachment_ids": [f"det_{det.detachment_id}" for det in right.detachments]},
    })


def test_reorganizations_with_shared_target_conserve_supplies(integrity_db):
    with create_session() as session:
        third = session.get(Army, 3)
        third.army_faction = "Blue"
        third.location_id = integrity_db["center"]
        session.commit()
        requests = [(0, _management_request(session, 0, 2, 200)), (2, _management_request(session, 2, 2, 200))]
    barrier = threading.Barrier(2)
    def apply(item):
        commander, request = item
        with create_session() as session:
            # Prime identity map before waiting, reproducing stale ORM reads.
            session.query(Army).all()
            barrier.wait(timeout=10)
            try:
                routes.apply_army_management(request, commander_id=commander, session=session, idempotency_key=f"manage-{commander}")
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(apply, requests))
    assert sorted(results) == [200, 409]
    with create_session() as session:
        assert sum(row.army_supply for row in session.query(Army)) == 300
        assert session.query(Detachment).count() == 4


def test_sequence_sync_preserves_advanced_and_empty_sequences(integrity_db):
    if integrity_db["backend"] != "postgresql":
        pytest.skip("PostgreSQL sequence semantics")
    with create_session() as session:
        sequence = session.execute(text("SELECT pg_get_serial_sequence('commanders', 'commander_id')")).scalar_one()
        session.execute(text("SELECT setval(CAST(:seq AS regclass), 500, true)"), {"seq": sequence})
        synchronize_sequences(session)
        row = Commander(commander_name="New", commander_title="Captain", commander_age=30)
        session.add(row)
        session.flush()
        assert row.commander_id == 501
        assert session.query(Message).count() == 0
        synchronize_sequences(session)
        assert session.execute(text("SELECT nextval(pg_get_serial_sequence('messages', 'message_id'))")).scalar_one() == 1


def test_fresh_migrations_and_complete_scenario_initialization(integrity_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from forwantofanail.core.database import Base
    from forwantofanail.core.initialize_db import initialize_database
    # Only the fixture's isolated database/schema is recreated.
    Base.metadata.drop_all(get_engine())
    command.upgrade(Config("alembic.ini"), "head")
    initialize_database()
    with create_session() as session:
        assert session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260913_0007"
        assert session.query(Army).filter_by(is_garrison=True).count() > 0
        initial_army_max = max(row.army_id for row in session.query(Army))
        initial_commander_max = max(row.commander_id for row in session.query(Commander))
        location = session.query(Army).first().location_id
        for i in range(3):
            commander = Commander(commander_name=f"Generated {i}", commander_title="Captain", commander_age=30)
            session.add(commander)
            session.flush()
            army = Army(commander_id=commander.commander_id, location_id=location, army_name=f"Generated {i}", army_faction="Blue")
            session.add(army)
            session.flush()
            session.add(Detachment(army_id=army.army_id, detachment_name="New soldiers", warrior_count=10))
            assert army.army_id > initial_army_max
            assert commander.commander_id > initial_commander_max
        session.commit()
        foreign_keys = inspect(get_engine()).get_foreign_keys("sieges")
        assert all("besieger_army_id" not in row["constrained_columns"] for row in foreign_keys)


def test_multiple_arrivals_draw_remain_at_origins(integrity_db, monkeypatch):
    monkeypatch.setattr(routes.random, "randint", lambda *_: 3)
    with create_session() as session:
        # Two Blue arrivals (100 each) meet a Red arrival (200).
        session.get(Detachment, 4).warrior_count = 0
        session.get(Detachment, 3).warrior_count = 200
        red_origin = next(cell for cell in h3.grid_ring(integrity_db["neighbor"], 1)
                          if cell != integrity_db["center"] and session.get(routes.Location, cell) is not None)
        session.get(Army, 3).location_id = red_origin
        origins = {row.army_id: row.location_id for row in session.query(Army)}
        for commander in range(3):
            session.add(Action(commander_id=commander, kind="move", state="in_progress", accepted_at=utcnow(),
                               parameters_json=json.dumps({"destination_h3": integrity_db["neighbor"], "remaining_cost": 1})))
        session.commit()
        _tick(session)
        battle = json.loads(session.query(WorldHistoryEvent).filter_by(event_kind="battle").one().payload_json)
        assert battle["winner_faction"] is None
        assert len(battle["participants"]) == 3
        assert {row.army_id: row.location_id for row in session.query(Army)} == origins
        assert session.query(Action).filter(Action.kind == "move", Action.state.in_(routes.ACTIVE_ACTION_STATES)).count() == 0


def test_contact_preserves_generated_rout_and_prevents_cooccupation(integrity_db, monkeypatch):
    monkeypatch.setattr(routes.random, "randint", lambda *_: 6)
    with create_session() as session:
        session.get(Army, 2).location_id = integrity_db["far"]
        session.get(Army, 3).location_id = integrity_db["neighbor"]
        session.get(Army, 1).army_morale = 2
        session.add(Action(commander_id=0, kind="move", state="in_progress", accepted_at=utcnow(),
                           parameters_json=json.dumps({"destination_h3": integrity_db["neighbor"], "remaining_cost": 1})))
        session.add(Action(commander_id=0, kind="move", state="queued", accepted_at=utcnow(), parameters_json="{}"))
        session.commit()
        _tick(session)
        assert session.query(Action).filter_by(commander_id=0, kind="rout", state="in_progress").count() == 1
        assert session.query(Action).filter(Action.commander_id == 0, Action.kind == "move", Action.state.in_(routes.ACTIVE_ACTION_STATES)).count() == 0
        assert not routes._execute_move_to_destination(session, session.get(GameClock, 1), session.get(Army, 1), integrity_db["neighbor"])
        assert session.get(Army, 1).location_id != session.get(Army, 3).location_id


@pytest.mark.parametrize("pair", ["reversed", "garrison"])
def test_reorganization_lock_order_for_reversed_pairs_and_garrison(integrity_db, pair):
    with create_session() as session:
        if pair == "garrison":
            third = session.get(Army, 3)
            third.location_id = integrity_db["center"]
            third.army_faction = "Blue"
            third.commander_id = None
            third.is_garrison = True
            third.garrison_stronghold_id = 1
            fort = session.get(routes.Stronghold, 1)
            fort.location_id = integrity_db["center"]
            fort.control = "Blue"
            session.commit()
            requests = [(0, _management_request(session, 0, 3, 100)), (1, _management_request(session, 1, 3, 100))]
            for _, request in requests:
                request.right_army.commander_id = None
                request.left_army.detachment_ids.append("det_3")
                request.right_army.detachment_ids = []
        else:
            requests = [(0, _management_request(session, 0, 2, 150)), (1, _management_request(session, 1, 1, 150))]
    barrier = threading.Barrier(2)
    def apply(item):
        commander, request = item
        with create_session() as session:
            barrier.wait(timeout=10)
            try:
                routes.apply_army_management(request, commander_id=commander, session=session, idempotency_key=f"pair-{commander}")
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(apply, requests)) == [200, 409]
    with create_session() as session:
        assert sum(row.army_supply for row in session.query(Army)) == 300
        assert session.query(Detachment).count() == 4


def test_stale_gameplay_authorization_is_rechecked_after_recovery(integrity_db):
    with create_session() as session:
        assign_agent(session, 0, "ollama_default")
        session.commit()
    old, token = worker.claim_run("old")
    with create_session() as session:
        from forwantofanail.agent_runtime.service import authenticate_run_session
        assert authenticate_run_session(session, token) == 0
        ctx = ToolContext(session, 0, f"agent-run:{old.run_id}", idempotency_key="gap", credential=token)
        arguments = {"state_token": services._state_fingerprint(ctx), "order": {"kind": "forage"}}
        session.rollback()
        with create_session() as recovery:
            recovery.get(AgentRun, old.run_id).lease_expires_at = utcnow() - timedelta(seconds=1)
            recovery.commit()
        worker.claim_run("replacement")
        with pytest.raises(services.ToolInvocationError) as caught:
            invoke("fwoan_submit_order", arguments, ctx)
        assert caught.value.status_code == 401
        assert session.query(Action).count() == 0
        assert session.query(IdempotencyRecord).count() == 0


def _claim_in_process(index):
    from forwantofanail.core.database import reset_database_runtime
    reset_database_runtime()
    result = worker.claim_run(f"process-{index}")
    return result[0] if result else None


def test_claiming_is_atomic_across_processes(integrity_db):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    with create_session() as session:
        assign_agent(session, 0, "ollama_default")
        session.commit()
    with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_claim_in_process, range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("admin_action", ["skip", "cancel", "disable"])
def test_admin_operations_serialize_with_worker_writes(integrity_db, admin_action):
    from forwantofanail.agent_runtime.service import cancel_and_requeue_run, disable_agent
    with create_session() as session:
        assign_agent(session, 0, "ollama_default")
        session.commit()
    lease, _ = worker.claim_run("worker")
    barrier = threading.Barrier(2)
    def write():
        barrier.wait(timeout=10)
        try:
            worker._record(lease, "racing_write", {})
        except LeaseLost:
            pass
    def admin():
        barrier.wait(timeout=10)
        with create_session() as session:
            def operation():
                if admin_action == "skip":
                    skip_run(session, 0, 0)
                elif admin_action == "cancel":
                    cancel_and_requeue_run(session, 0, 0)
                else:
                    disable_agent(session, 0)
            routes._run_world_mutation(session, operation)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(write), pool.submit(admin)]
        for future in futures:
            future.result(timeout=20)
    with pytest.raises(LeaseLost):
        worker._record(lease, "after_admin", {})
    with create_session() as session:
        events = session.query(AgentRunEvent).filter_by(run_id=lease.run_id).order_by(AgentRunEvent.sequence).all()
        assert len({event.sequence for event in events}) == len(events)
        assert events[-1].event_kind in {"skipped", "obsolete"}


def test_march_options_do_not_reveal_unseen_enemy(integrity_db, monkeypatch):
    monkeypatch.setattr(routes, "_environs_radius_for_army", lambda army: 0)
    with create_session() as session:
        army = session.get(Army, 1)
        enemy = session.get(Army, 3)
        enemy.location_id = integrity_db["neighbor"]
        session.commit()
        ctx = ToolContext(session, 0, "visibility")
        assert integrity_db["neighbor"] in services._legal_next_cells(ctx, army, [])
        options = routes.get_valid_next_destinations(staged_path=None, commander_id=0, session=session)
        assert integrity_db["neighbor"] in options["valid_destinations"]
        routes._validate_known_march_occupancy(session, army, [integrity_db["neighbor"]])
        monkeypatch.setattr(routes, "_environs_radius_for_army", lambda army: 1)
        assert integrity_db["neighbor"] not in services._legal_next_cells(ctx, army, [])


def test_victorious_mover_enters_after_retreat(integrity_db, monkeypatch):
    monkeypatch.setattr(routes.random, "randint", lambda *_: 3)
    with create_session() as session:
        session.get(Army, 2).location_id = integrity_db["far"]
        session.get(Army, 3).location_id = integrity_db["neighbor"]
        session.get(Army, 3).army_morale = 2
        session.add(Action(commander_id=0, kind="move", state="in_progress", accepted_at=utcnow(),
                           parameters_json=json.dumps({"destination_h3": integrity_db["neighbor"], "remaining_cost": 1})))
        session.commit()
        _tick(session)
        assert session.get(Army, 1).location_id == integrity_db["neighbor"]
        assert session.get(Army, 3).location_id != integrity_db["neighbor"]
        assert session.query(WorldHistoryEvent).filter_by(event_kind="battle").count() == 1


def test_receipt_insert_failure_rolls_back_gameplay(integrity_db):
    from sqlalchemy import event
    with create_session() as session:
        ctx = ToolContext(session, 0, "receipt", idempotency_key="receipt-insert")
        args = {"state_token": services._state_fingerprint(ctx), "order": {"kind": "forage"}}
        def fail_insert(*_):
            raise RuntimeError("injected receipt insert failure")
        event.listen(IdempotencyRecord, "before_insert", fail_insert)
        try:
            with pytest.raises(RuntimeError, match="receipt insert"):
                invoke("fwoan_submit_order", args, ctx)
        finally:
            event.remove(IdempotencyRecord, "before_insert", fail_insert)
        assert session.query(Action).count() == 0
        assert session.query(routes.Alert).count() == 0
        assert session.query(IdempotencyRecord).count() == 0
        response = invoke("fwoan_submit_order", args, ctx)
        assert invoke("fwoan_submit_order", args, ctx) == response
        changed = dict(args, order={"kind": "hold"})
        with pytest.raises(services.ToolInvocationError) as caught:
            invoke("fwoan_submit_order", changed, ctx)
        assert caught.value.status_code == 409


def test_provider_response_after_recovery_is_discarded(integrity_db, monkeypatch):
    from forwantofanail.agent_runtime.providers import ModelTurn
    from forwantofanail.agent_runtime.context import load_profiles
    with create_session() as session:
        assign_agent(session, 0, "ollama_default")
        session.commit()
    old, token = worker.claim_run("old")
    replacement = []
    class InterruptedProvider:
        def invoke(self, *args):
            with create_session() as session:
                session.get(AgentRun, old.run_id).lease_expires_at = utcnow() - timedelta(seconds=1)
                session.commit()
            replacement.append(worker.claim_run("new")[0])
            return ModelTurn(content="Late old response", tool_calls=[], input_tokens=100, output_tokens=100, finish_reason="stop")
    monkeypatch.setattr(worker, "_load_context", lambda *_: (load_profiles()["ollama_default"], [], []))
    monkeypatch.setattr(worker, "adapter_for", lambda *_: InterruptedProvider())
    worker.execute_run(old, token)
    with create_session() as session:
        run = session.get(AgentRun, old.run_id)
        assert run.status == "running" and run.lease_generation == replacement[0].generation
        assert run.input_tokens == 0 and run.output_tokens == 0
        assert session.query(AgentRunEvent).filter_by(event_kind="model_response").count() == 0
    with pytest.raises(LeaseLost):
        worker._runtime_call(old, ModelToolCall("stale-memory", "fwoan_update_scratchpad", {"expected_revision": 1, "content": "stale"}))


@pytest.mark.parametrize("competitor", ["order", "watch"])
def test_reorganization_serializes_with_orders_and_watch(integrity_db, competitor):
    from forwantofanail.api.schemas import TimeAdvanceRequest
    with create_session() as session:
        request = _management_request(session, 0, 2, 150)
    barrier = threading.Barrier(2)
    def reorganize():
        with create_session() as session:
            session.query(Army).all()
            barrier.wait(timeout=10)
            try:
                routes.apply_army_management(request, commander_id=0, session=session, idempotency_key="racing-management")
                return 200
            except HTTPException as exc:
                return exc.status_code
    def competing():
        with create_session() as session:
            barrier.wait(timeout=10)
            if competitor == "order":
                routes.plan_actions(ActionPlanRequest(kind="forage"), commander_id=1, session=session, idempotency_key="racing-order")
            else:
                routes.advance_time_for_development(TimeAdvanceRequest(steps=1, execute_actions=False), session=session,
                                                    x_admin_token="integrity-admin", idempotency_key="racing-watch")
    with ThreadPoolExecutor(2) as pool:
        first, second = pool.submit(reorganize), pool.submit(competing)
        assert first.result(timeout=20) in {200, 409}
        second.result(timeout=20)
    with create_session() as session:
        assert sum(army.army_supply for army in session.query(Army)) == 300
        assert session.query(Detachment).count() == 4


def test_destroyed_battle_participant_is_not_mutated_again(integrity_db, monkeypatch):
    monkeypatch.setattr(routes.random, "randint", lambda *_: 3)
    monkeypatch.setattr(routes, "list_valid_destinations", lambda *_: [])
    with create_session() as session:
        siege_id = _siege(session, False)
        session.get(Army, 1).army_morale = 2
        session.get(Army, 3).army_morale = 12
        action = Action(commander_id=2, kind="attack", state="in_progress", accepted_at=utcnow(), parameters_json="{}")
        session.add(action)
        session.flush()
        clock = session.get(GameClock, 1)
        clock.world_tick = 1
        clock.watch = 2
        routes._resolve_battles_from_edges(
            session, clock, action_by_id={action.action_id: action},
            edges=[(action.action_id, 3, 1)], target_h3_by_action_id={action.action_id: integrity_db["center"]},
            target_army_id_by_action_id={action.action_id: 1}, engagement_type="siege",
        )
        session.commit()
        assert session.get(Army, 1) is None
        assert session.get(Siege, siege_id).besieger_army_id == 1
        assert session.get(Siege, siege_id).live_besieger_army_id is None
        assert session.query(WorldHistoryEvent).filter_by(event_kind="battle").count() == 1
        assert session.query(WorldHistoryEvent).filter_by(event_kind="army_destroyed").count() == 1
        assert session.query(routes.AlertRecipient).filter_by(commander_id=0).count() > 0
        assert session.query(Action).filter_by(commander_id=0, kind="rout").count() == 0


def test_mcp_changed_payload_with_same_request_id_conflicts(integrity_db):
    raw = "mcp-conflict"
    with create_session() as session:
        session.add(AuthToken(token=hashlib.sha256(raw.encode()).hexdigest(), commander_id=0, created_at=utcnow(), last_used_at=utcnow(), client_kind="api"))
        session.commit()
        arguments = {"state_token": services._state_fingerprint(ToolContext(session, 0, hashlib.sha256(raw.encode()).hexdigest())), "order": {"kind": "forage"}}
    with TestClient(app) as client:
        envelope = {"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {"name": "fwoan_submit_order", "arguments": arguments}}
        first = client.post("/mcp", json=envelope, headers={"Authorization": f"Bearer {raw}"}).json()
        assert not first["result"]["isError"], first
        arguments["order"] = {"kind": "hold"}
        second = client.post("/mcp", json=envelope, headers={"Authorization": f"Bearer {raw}"}).json()
        assert second["result"]["isError"]
        assert "different request" in second["result"]["content"][0]["text"]


def test_winning_faction_does_not_override_individual_retreat(integrity_db, monkeypatch):
    monkeypatch.setattr(routes.random, "choice", lambda choices: next((value for value in choices if value != integrity_db["neighbor"]), choices[0]))
    dice = iter([1, 1, 6, 6, 3, 3])
    monkeypatch.setattr(routes.random, "randint", lambda *_: next(dice, 1))
    with create_session() as session:
        session.get(Army, 1).army_morale = 2
        session.get(Army, 2).army_morale = 12
        session.get(Detachment, 4).warrior_count = 0
        session.get(Detachment, 3).warrior_count = 200
        red_origin = next(cell for cell in h3.grid_ring(integrity_db["neighbor"], 1)
                          if cell != integrity_db["center"] and session.get(routes.Location, cell) is not None)
        session.get(Army, 3).location_id = red_origin
        for commander in range(3):
            session.add(Action(commander_id=commander, kind="move", state="in_progress", accepted_at=utcnow(),
                               parameters_json=json.dumps({"destination_h3": integrity_db["neighbor"], "remaining_cost": 1})))
        session.commit()
        _tick(session)
        battle = json.loads(session.query(WorldHistoryEvent).filter_by(event_kind="battle").one().payload_json)
        assert battle["winner_faction"] == "Blue"
        weak = next(row for row in battle["participants"] if row["before"]["army_id"] == 1)
        assert weak["retreat"]["retreated"]
        assert session.get(Army, 1).location_id != integrity_db["neighbor"]
        assert session.get(Army, 2).location_id == integrity_db["neighbor"]


def test_contact_halts_defender_with_unfinished_march_leg(integrity_db, monkeypatch):
    monkeypatch.setattr(routes.random, "randint", lambda *_: 3)
    with create_session() as session:
        session.get(Army, 2).location_id = integrity_db["far"]
        session.get(Army, 3).location_id = integrity_db["neighbor"]
        session.add(Action(commander_id=0, kind="move", state="in_progress", accepted_at=utcnow(),
                           parameters_json=json.dumps({"destination_h3": integrity_db["neighbor"], "remaining_cost": 1})))
        session.add(Action(commander_id=2, kind="move", state="in_progress", accepted_at=utcnow(),
                           parameters_json=json.dumps({"destination_h3": integrity_db["far"], "remaining_cost": 3})))
        session.add(Action(commander_id=2, kind="move", state="queued", accepted_at=utcnow(), parameters_json="{}"))
        session.add(StandingOrder(commander_id=2, follow_road_enabled=True, updated_at=utcnow()))
        session.commit()
        _tick(session)
        assert session.query(WorldHistoryEvent).filter_by(event_kind="battle").count() == 1
        assert session.query(Action).filter(Action.commander_id == 2, Action.kind == "move", Action.state.in_(routes.ACTIVE_ACTION_STATES)).count() == 0
        assert not session.get(StandingOrder, 2).follow_road_enabled
