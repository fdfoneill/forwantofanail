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


def test_admin_army_summary_reports_commander_armies(sqlite_db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "summary-secret")
    session = create_session()
    try:
        rows = routes.admin_armies_summary(session=session, x_admin_token="summary-secret")
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
        assert gain == 3611
        assert {location.location_id for location in forageable_locations} == {"origin_1", "origin_2", "fort_1"}
        assert average_depletion == 1

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
        ] == [1, 2, 3]
        alert = session.query(Alert).filter(Alert.recipient_commander_id == 1).order_by(Alert.alert_id.desc()).first()
        assert alert is not None
        assert "foraged from picked-over country" in alert.message
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
