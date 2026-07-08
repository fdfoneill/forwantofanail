from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from forwantofanail.api import routes
from forwantofanail.api.schemas import (
    ActionCreateRequest,
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
    Army,
    AuthToken,
    Commander,
    CommanderClaim,
    Detachment,
    Location,
    Message,
    Siege,
    StandingOrder,
    Stronghold,
    TerrainType,
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
        assert session.query(Siege).filter(Siege.state == "active").count() == 1
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
        assert by_id["cmd_2"]["claimed"] is False
        assert by_id["cmd_2"]["faction"] == "Beta"
    finally:
        session.close()


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


def test_admin_army_summary_reports_commander_armies(sqlite_db):
    session = create_session()
    try:
        rows = routes.admin_armies_summary(session=session, x_admin_token=None)
        by_army = {row["army_name"]: row for row in rows}
        assert by_army["Alpha Host"]["commander_name"] == "Lord Alpha"
        assert by_army["Alpha Host"]["faction"] == "Alpha"
        assert by_army["Alpha Host"]["claimed"] is False
        assert by_army["Alpha Host"]["strength"] == 100
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
