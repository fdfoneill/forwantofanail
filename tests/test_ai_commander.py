from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

_TMPDIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMPDIR.name) / 'test_ai_commander.db'}"

from forwantofanail.ai_commander import CommanderApiClient, CommanderApiError, CommanderToolRegistry
from forwantofanail.ai_commander.models import TransportResponse

try:
    from forwantofanail.ai_commander.runtime import (
        CommanderHeartbeatScheduler,
        CommanderWorker,
        LocalArtifactLogger,
        RuntimeConfig,
        current_action_fingerprint,
        evaluate_runtime_attention,
        get_runtime_detail,
        load_commander_dossier,
        list_runs,
        list_runtime_rows,
        mark_manual_attention,
        runtime_for_commander,
        set_controller_type,
    )

    RUNTIME_DEPS_AVAILABLE = True
except Exception:
    CommanderHeartbeatScheduler = None
    CommanderWorker = None
    LocalArtifactLogger = None
    RuntimeConfig = None
    current_action_fingerprint = None
    evaluate_runtime_attention = None
    get_runtime_detail = None
    load_commander_dossier = None
    list_runs = None
    list_runtime_rows = None
    mark_manual_attention = None
    runtime_for_commander = None
    set_controller_type = None
    RUNTIME_DEPS_AVAILABLE = False

try:
    from fastapi.testclient import TestClient
    from forwantofanail.api.app import app
    from forwantofanail.core.initialize_db import initialize_database
    from forwantofanail.core.database import create_session

    INTEGRATION_DEPS_AVAILABLE = True
except Exception:
    TestClient = None
    app = None
    initialize_database = None
    create_session = None
    INTEGRATION_DEPS_AVAILABLE = False

FULL_RUNTIME_DEPS_AVAILABLE = INTEGRATION_DEPS_AVAILABLE and RUNTIME_DEPS_AVAILABLE


DATA_DIR = Path(__file__).resolve().parents[1] / "forwantofanail" / "data"


class TestClientTransport:
    def __init__(self, client: TestClient):
        self.client = client

    def request(self, *, method, url, headers=None, params=None, json_body=None, timeout=None):
        _ = timeout
        split = urlsplit(url)
        response = self.client.request(
            method,
            split.path,
            headers=headers,
            params=params,
            json=json_body,
        )
        content_type = response.headers.get("content-type", "")
        data = response.json() if "application/json" in content_type else response.text
        return TransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            data=data,
        )


class StubTransport:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.payload = payload

    def request(self, *, method, url, headers=None, params=None, json_body=None, timeout=None):
        _ = method, url, headers, params, json_body, timeout
        return TransportResponse(
            status_code=self.status_code,
            headers={"content-type": "application/json"},
            data=self.payload,
        )


class DummyClient:
    def list_correspondents(self):
        return [{"id": "cmd_2", "name": "Giubrin"}]

    def list_messages(self, unread_only: bool = False):
        return [{"id": "msg_1", "is_read": not unread_only}]

    def read_message(self, message_id: str):
        return {"id": message_id, "content": "Scout report"}

    def send_message(self, recipient_id: str, content: str, priority: str = "normal"):
        return {"message_id": "msg_2", "recipient_id": recipient_id, "content": content, "priority": priority}

    def get_current_action(self):
        return None

    def cancel_action(self, action_id: str):
        return {"action_id": action_id, "state": "cancelled"}

    def create_action(self, **kwargs):
        if kwargs.get("kind") == "move" and not kwargs.get("destination_h3"):
            raise CommanderApiError(
                message="destination_h3 is required for move actions",
                endpoint="/v1/me/actions",
                method="POST",
                http_status=400,
                raw_detail="destination_h3 is required for move actions",
                error_code="invalid_request",
                retryable=False,
            )
        return {"action_id": "act_1", **kwargs}

    def plan_actions(self, *, kind: str, path: list[str]):
        return {"kind": kind, "path": path, "hold": kind == "march" and not path}

    def get_valid_next_destinations(self, *, origin_h3: str | None = None):
        return {"origin_h3": origin_h3, "valid_destinations": ["cell_1"]}

    def get_valid_attack_targets(self, *, origin_h3: str | None = None):
        return {"origin_h3": origin_h3, "targets": []}

    def get_valid_besiege_targets(self, *, origin_h3: str | None = None):
        return {"origin_h3": origin_h3, "targets": []}

    def get_standing_orders(self):
        return {"follow_road": {"enabled": False}, "forced_march": {"enabled": False}}

    def set_follow_road(self, *, enabled: bool):
        return {"follow_road": {"enabled": enabled}}

    def set_forced_march(self, *, enabled: bool):
        return {"forced_march": {"enabled": enabled}}

    def list_alerts(self, *, limit: int = 25, unread_only: bool = False):
        return [{"id": "alt_1", "limit": limit, "unread_only": unread_only}]

    def get_border_roads(self, *, cells: list[str]):
        return {"roads": cells}

    def list_known_strongholds(
        self,
        *,
        stronghold_id: str | None = None,
        faction: str | None = None,
        region: str | None = None,
        search: str | None = None,
    ):
        return {
            "strongholds": [
                {
                    "stronghold_id": stronghold_id or "sh_1",
                    "stronghold_name": search or "Kumba",
                    "faction": faction or "Delisgar",
                    "region": region or "West",
                    "location_h3": "cell_1",
                    "stronghold_type": "town",
                }
            ]
        }

    def get_stronghold_route(
        self,
        *,
        from_stronghold_id: str,
        to_stronghold_id: str,
        avoid_stronghold_ids: list[str] | None = None,
        on_road: bool = True,
    ):
        return {
            "from": {"stronghold_id": from_stronghold_id},
            "to": {"stronghold_id": to_stronghold_id},
            "avoid_stronghold_ids": avoid_stronghold_ids or [],
            "path_h3": ["cell_1", "cell_2"],
            "path_length": 2,
            "path_steps": 1,
            "on_road_only": on_road,
            "offroad_allowed": True,
            "used_offroad": not on_road,
            "total_cost": 1 if on_road else 2,
        }


class AiCommanderUnitOnlyTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = CommanderToolRegistry(DummyClient())

    def test_tool_schema_shape(self):
        tools = self.registry.get_tools()
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(tool["type"] == "function" for tool in tools))
        self.assertTrue(all(tool["strict"] is True for tool in tools))
        self.assertTrue(all(tool["parameters"]["type"] == "object" for tool in tools))
        self.assertTrue(all(tool["parameters"]["additionalProperties"] is False for tool in tools))
        self.assertTrue(
            all(
                sorted(tool["parameters"]["required"]) == sorted(tool["parameters"]["properties"].keys())
                for tool in tools
            )
        )
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            by_name["create_action"]["parameters"]["required"],
            ["kind", "destination_h3", "target_h3", "target_army_id", "target_stronghold_id"],
        )
        self.assertEqual(by_name["send_message"]["parameters"]["required"], ["recipient_id", "content", "priority"])
        self.assertEqual(by_name["plan_actions"]["parameters"]["required"], ["kind", "path"])
        self.assertEqual(by_name["list_messages"]["parameters"]["required"], ["unread_only"])
        self.assertEqual(by_name["get_valid_next_destinations"]["parameters"]["properties"]["origin_h3"]["type"], ["string", "null"])
        self.assertEqual(
            by_name["list_known_strongholds"]["parameters"]["required"],
            ["stronghold_id", "faction", "region", "search"],
        )
        self.assertEqual(
            by_name["get_stronghold_route"]["parameters"]["required"],
            ["from_stronghold_id", "to_stronghold_id", "avoid_stronghold_ids", "on_road"],
        )

    def test_dispatch_success_and_failure(self):
        success = self.registry.dispatch("list_correspondents")
        self.assertTrue(success.ok)
        self.assertEqual(success.data["result"][0]["id"], "cmd_2")

        current_action = self.registry.dispatch("get_current_action")
        self.assertTrue(current_action.ok)
        self.assertIsNone(current_action.data["result"])

        failure = self.registry.dispatch("create_action", {"kind": "move"})
        self.assertFalse(failure.ok)
        self.assertEqual(failure.error["http_status"], 400)
        self.assertEqual(failure.error["error_code"], "invalid_request")

    def test_geography_tools(self):
        lookup = self.registry.dispatch("list_known_strongholds", {"faction": "Delisgar"})
        self.assertTrue(lookup.ok)
        self.assertEqual(lookup.data["result"]["strongholds"][0]["faction"], "Delisgar")

        route = self.registry.dispatch(
            "get_stronghold_route",
            {
                "from_stronghold_id": "sh_1",
                "to_stronghold_id": "sh_2",
                "avoid_stronghold_ids": ["sh_3"],
                "on_road": True,
            },
        )
        self.assertTrue(route.ok)
        self.assertEqual(route.data["result"]["path_h3"], ["cell_1", "cell_2"])

    def test_example_smoke_function_call_round_trip(self):
        from examples.ai_commander_responses_example import execute_response_function_calls

        response = SimpleNamespace(
            output=[
                SimpleNamespace(type="function_call", name="list_correspondents", arguments="{}", call_id="call_1"),
                SimpleNamespace(
                    type="function_call",
                    name="set_follow_road",
                    arguments=json.dumps({"enabled": True}),
                    call_id="call_2",
                ),
            ]
        )
        outputs = execute_response_function_calls(response, self.registry)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(json.loads(outputs[0]["output"])["ok"], True)
        self.assertEqual(json.loads(outputs[1]["output"])["ok"], True)


@unittest.skipUnless(INTEGRATION_DEPS_AVAILABLE, "fastapi/sqlalchemy test dependencies are unavailable")
class AiCommanderTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = TestClient(app)

    def setUp(self):
        initialize_database(DATA_DIR, reset=True)
        self.transport = TestClientTransport(self.http)
        self.sofonisba = CommanderApiClient(
            base_url="http://testserver",
            commander_name="Sofonisba",
            transport=self.transport,
        )
        self.soolabab = CommanderApiClient(
            base_url="http://testserver",
            commander_name="Soolabab",
            transport=self.transport,
        )

    def _advance_time(self, steps: int):
        response = self.http.post("/v1/admin/time/advance", json={"steps": steps, "execute_actions": True})
        self.assertEqual(response.status_code, 200, response.text)

    def test_login_flow_from_commander_name(self):
        self.assertTrue(self.sofonisba.token)
        correspondents = self.sofonisba.list_correspondents()
        self.assertEqual(len(correspondents), 3)
        self.assertEqual({row["name"] for row in correspondents}, {"Soolabab", "Giubrin", "Thurum II"})

    def test_authenticated_reads_and_writes(self):
        sent = self.soolabab.send_message("cmd_1", "Hold the river line.", priority="high")
        self.assertEqual(sent["status"], "in_transit")
        self._advance_time(3)
        inbox = self.sofonisba.list_messages(unread_only=True)
        self.assertTrue(inbox)
        message = self.sofonisba.read_message(inbox[0]["id"])
        self.assertEqual(message["content"], "Hold the river line.")
        self.assertTrue(message["is_read"])

    def test_geography_lookup_and_route(self):
        strongholds = self.sofonisba.list_known_strongholds()
        self.assertTrue(strongholds["strongholds"])
        ordered_ids = [row["stronghold_id"] for row in strongholds["strongholds"]]
        self.assertEqual(
            ordered_ids,
            sorted(ordered_ids, key=lambda value: int(value.split("_", 1)[1])),
        )

        boonan_only = self.sofonisba.list_known_strongholds(faction="Boonan")
        self.assertTrue(boonan_only["strongholds"])
        self.assertTrue(all(row["faction"].lower() == "boonan" for row in boonan_only["strongholds"]))

        named = self.sofonisba.list_known_strongholds(search="kum")
        self.assertTrue(named["strongholds"])
        self.assertTrue(any("kum" in row["stronghold_name"].lower() for row in named["strongholds"]))

        route = self.sofonisba.get_stronghold_route(
            from_stronghold_id=strongholds["strongholds"][0]["stronghold_id"],
            to_stronghold_id=strongholds["strongholds"][-1]["stronghold_id"],
            avoid_stronghold_ids=[],
            on_road=True,
        )
        self.assertGreaterEqual(route["path_length"], 2)
        self.assertEqual(route["path_h3"][0], route["from"]["location_h3"])
        self.assertEqual(route["path_h3"][-1], route["to"]["location_h3"])

    def test_geography_route_errors_and_avoidance(self):
        strongholds = self.sofonisba.list_known_strongholds()["strongholds"]
        with self.assertRaises(CommanderApiError) as exc:
            self.sofonisba.get_stronghold_route(
                from_stronghold_id="sh_9999",
                to_stronghold_id=strongholds[0]["stronghold_id"],
            )
        self.assertEqual(exc.exception.http_status, 400)

        if len(strongholds) >= 3:
            route = self.sofonisba.get_stronghold_route(
                from_stronghold_id=strongholds[0]["stronghold_id"],
                to_stronghold_id=strongholds[-1]["stronghold_id"],
                avoid_stronghold_ids=[strongholds[1]["stronghold_id"]],
                on_road=False,
            )
            blocked_location = strongholds[1]["location_h3"]
            if route["path_length"] > 2:
                self.assertNotIn(blocked_location, route["path_h3"][1:-1])

    def test_error_normalization_by_status(self):
        invalid_auth_client = CommanderApiClient(
            base_url="http://testserver",
            token="bad-token",
            transport=self.transport,
        )
        with self.assertRaises(CommanderApiError) as auth_exc:
            invalid_auth_client.list_correspondents()
        self.assertEqual(auth_exc.exception.http_status, 401)
        self.assertEqual(auth_exc.exception.error_code, "authentication_error")

        for status_code, expected_code in [
            (400, "invalid_request"),
            (404, "not_found"),
            (409, "conflict"),
            (422, "unprocessable_entity"),
        ]:
            client = CommanderApiClient(
                base_url="http://testserver",
                token="token",
                transport=StubTransport(status_code, {"detail": "problem"}),
            )
            with self.assertRaises(CommanderApiError) as exc:
                client.list_correspondents()
            self.assertEqual(exc.exception.http_status, status_code)
            self.assertEqual(exc.exception.error_code, expected_code)

    def test_tool_schema_shape(self):
        registry = CommanderToolRegistry(self.sofonisba)
        tools = registry.get_tools()
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(tool["type"] == "function" for tool in tools))
        self.assertTrue(all(tool["strict"] is True for tool in tools))
        self.assertTrue(all(tool["parameters"]["type"] == "object" for tool in tools))
        self.assertTrue(all(tool["parameters"]["additionalProperties"] is False for tool in tools))
        self.assertTrue(
            all(
                sorted(tool["parameters"]["required"]) == sorted(tool["parameters"]["properties"].keys())
                for tool in tools
            )
        )
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            by_name["create_action"]["parameters"]["required"],
            ["kind", "destination_h3", "target_h3", "target_army_id", "target_stronghold_id"],
        )
        self.assertEqual(by_name["send_message"]["parameters"]["required"], ["recipient_id", "content", "priority"])
        self.assertEqual(by_name["plan_actions"]["parameters"]["required"], ["kind", "path"])

    def test_tool_handlers_for_messages_and_delivery_estimate(self):
        registry = CommanderToolRegistry(self.soolabab)
        sent = registry.dispatch("send_message", {"recipient_id": "cmd_1", "content": "Ride at dawn.", "priority": "high"})
        self.assertTrue(sent.ok)
        sent_result = sent.data["result"]
        self.assertEqual(sent_result["status"], "in_transit")
        self.assertIn("estimated_delivery_watch", sent_result)

        self._advance_time(3)
        inbox = CommanderToolRegistry(self.sofonisba).dispatch("list_messages", {"unread_only": True})
        self.assertTrue(inbox.ok)
        message_id = inbox.data["result"][0]["id"]
        read = CommanderToolRegistry(self.sofonisba).dispatch("read_message", {"message_id": message_id})
        self.assertTrue(read.ok)
        self.assertEqual(read.data["result"]["content"], "Ride at dawn.")

    def test_create_action_failure_preserves_api_error_details(self):
        registry = CommanderToolRegistry(self.sofonisba)

        invalid_move = registry.dispatch("create_action", {"kind": "move"})
        self.assertFalse(invalid_move.ok)
        self.assertEqual(invalid_move.error["http_status"], 400)
        self.assertIn("destination_h3", invalid_move.error["message"])

        invalid_attack = registry.dispatch("create_action", {"kind": "attack"})
        self.assertFalse(invalid_attack.ok)
        self.assertEqual(invalid_attack.error["http_status"], 400)
        self.assertIn("target_h3", invalid_attack.error["message"])

        invalid_besiege = registry.dispatch("create_action", {"kind": "besiege"})
        self.assertFalse(invalid_besiege.ok)
        self.assertEqual(invalid_besiege.error["http_status"], 400)
        self.assertIn("target_stronghold_id", invalid_besiege.error["message"])

    def test_plan_actions_and_standing_orders(self):
        registry = CommanderToolRegistry(self.sofonisba)
        valid_next = registry.dispatch("get_valid_next_destinations")
        self.assertTrue(valid_next.ok)
        first_destination = valid_next.data["result"]["valid_destinations"][0]

        march = registry.dispatch("plan_actions", {"kind": "march", "path": [first_destination]})
        self.assertTrue(march.ok)
        self.assertFalse(march.data["result"]["hold"])
        self.assertEqual(len(march.data["result"]["created"]), 1)

        hold = registry.dispatch("plan_actions", {"kind": "march", "path": []})
        self.assertTrue(hold.ok)
        self.assertTrue(hold.data["result"]["hold"])

        forage = registry.dispatch("plan_actions", {"kind": "forage", "path": []})
        self.assertTrue(forage.ok)
        self.assertEqual(forage.data["result"]["kind"], "forage")

        follow_road = registry.dispatch("set_follow_road", {"enabled": True})
        self.assertTrue(follow_road.ok)
        self.assertTrue(follow_road.data["result"]["follow_road"]["enabled"])

        forced_march = registry.dispatch("set_forced_march", {"enabled": True})
        self.assertTrue(forced_march.ok)
        self.assertTrue(forced_march.data["result"]["forced_march"]["enabled"])

    def test_example_smoke_function_call_round_trip(self):
        from examples.ai_commander_responses_example import execute_response_function_calls

        registry = CommanderToolRegistry(self.sofonisba)
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="list_correspondents",
                    arguments="{}",
                    call_id="call_1",
                ),
                SimpleNamespace(
                    type="function_call",
                    name="set_follow_road",
                    arguments=json.dumps({"enabled": True}),
                    call_id="call_2",
                ),
            ]
        )
        outputs = execute_response_function_calls(response, registry)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0]["type"], "function_call_output")
        first_output = json.loads(outputs[0]["output"])
        second_output = json.loads(outputs[1]["output"])
        self.assertTrue(first_output["ok"])
        self.assertTrue(second_output["ok"])


@unittest.skipUnless(FULL_RUNTIME_DEPS_AVAILABLE, "runtime integration dependencies are unavailable")
class AiCommanderHeartbeatTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = TestClient(app)

    def setUp(self):
        self.logs_dir = Path(_TMPDIR.name) / "logs"
        initialize_database(DATA_DIR, reset=True)
        self.transport = TestClientTransport(self.http)
        self.config = RuntimeConfig(base_url="http://testserver", log_dir=self.logs_dir, lease_duration_seconds=1)

    def _make_client(self, commander_name: str):
        return CommanderApiClient(
            base_url="http://testserver",
            commander_name=commander_name,
            transport=self.transport,
        )

    def _advance_time(self, steps: int):
        response = self.http.post("/v1/admin/time/advance", json={"steps": steps, "execute_actions": True})
        self.assertEqual(response.status_code, 200, response.text)

    def test_runtime_model_and_controller_transitions(self):
        session = create_session()
        try:
            runtimes = list_runtime_rows(session)
            self.assertEqual(len(runtimes), 4)
            self.assertTrue(all(row["controller_type"] == "human" for row in runtimes))
            runtime = set_controller_type(session, 1, "ai")
            session.commit()
            self.assertEqual(runtime.controller_type, "ai")
            self.assertTrue(runtime.ai_enabled)
            runtime = set_controller_type(session, 1, "disabled")
            session.commit()
            self.assertEqual(runtime.controller_type, "disabled")
            self.assertFalse(runtime.ai_enabled)
        finally:
            session.close()

    def test_attention_detection_for_clock_messages_and_actions(self):
        session = create_session()
        try:
            runtime = set_controller_type(session, 1, "ai")
            session.commit()
            evaluation = evaluate_runtime_attention(session, runtime)
            self.assertIn("startup_reconcile", evaluation["reasons"])
        finally:
            session.close()

        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            evaluation = evaluate_runtime_attention(session, runtime)
            runtime.last_reviewed_day = evaluation["clock"].day
            runtime.last_reviewed_watch = evaluation["clock"].watch
            runtime.last_reviewed_message_id = evaluation["latest_message_id"]
            runtime.last_reviewed_action_fingerprint = evaluation["action_fingerprint"]
            runtime.attention_needed = False
            runtime.attention_reasons_json = "[]"
            session.commit()
        finally:
            session.close()

        self._advance_time(1)
        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            evaluation = evaluate_runtime_attention(session, runtime)
            self.assertIn("clock_advanced", evaluation["reasons"])
        finally:
            session.close()

        soolabab = self._make_client("Soolabab")
        soolabab.send_message("cmd_1", "Courier for you.")
        self._advance_time(3)
        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            evaluation = evaluate_runtime_attention(session, runtime)
            self.assertIn("message_received", evaluation["reasons"])
        finally:
            session.close()

        sofonisba = self._make_client("Sofonisba")
        next_destinations = sofonisba.get_valid_next_destinations()
        sofonisba.create_action(kind="move", destination_h3=next_destinations["valid_destinations"][0])
        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            runtime.last_reviewed_action_fingerprint = "stale"
            evaluation = evaluate_runtime_attention(session, runtime)
            self.assertIn("action_state_changed", evaluation["reasons"])
        finally:
            session.close()

    def test_scheduler_coalesces_and_respects_leases(self):
        launched: list[tuple[int, int, str]] = []

        def fake_launcher(**kwargs):
            launched.append((kwargs["commander_id"], kwargs["run_id"], kwargs["lease_token"]))
            return None

        session = create_session()
        try:
            set_controller_type(session, 1, "ai")
            mark_manual_attention(session, 1)
            mark_manual_attention(session, 1, "message_received")
            set_controller_type(session, 2, "human")
            session.commit()
        finally:
            session.close()

        scheduler = CommanderHeartbeatScheduler(config=self.config, worker_launcher=fake_launcher)
        launched_run_ids = scheduler.run_once()
        self.assertEqual(len(launched_run_ids), 1)
        self.assertEqual(len(launched), 1)

        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            detail = get_runtime_detail(session, 1)
            self.assertEqual(runtime.active_run_id, launched_run_ids[0])
            self.assertEqual(detail["recent_runs"][0]["status"], "queued")
        finally:
            session.close()

        second_pass = scheduler.run_once()
        self.assertEqual(second_pass, [])

    def test_expired_lease_is_recoverable(self):
        launched: list[int] = []

        def fake_launcher(**kwargs):
            launched.append(kwargs["run_id"])
            return None

        session = create_session()
        try:
            set_controller_type(session, 1, "ai")
            mark_manual_attention(session, 1)
            session.commit()
        finally:
            session.close()

        scheduler = CommanderHeartbeatScheduler(config=self.config, worker_launcher=fake_launcher)
        first = scheduler.run_once()
        self.assertEqual(len(first), 1)

        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            runtime.lease_expires_at = runtime.lease_expires_at.replace(year=2000)
            session.commit()
        finally:
            session.close()

        second = scheduler.run_once()
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0], second[0])

    def test_worker_persists_summary_and_logs(self):
        session = create_session()
        try:
            set_controller_type(session, 1, "ai")
            mark_manual_attention(session, 1)
            session.commit()
        finally:
            session.close()

        launched = []

        def fake_launcher(**kwargs):
            launched.append(kwargs)
            return None

        scheduler = CommanderHeartbeatScheduler(config=self.config, worker_launcher=fake_launcher)
        run_ids = scheduler.run_once()
        self.assertEqual(len(run_ids), 1)
        launch = launched[0]

        worker = CommanderWorker(
            config=self.config,
            turn_runner=SimpleNamespace(
                run_turn=lambda **kwargs: {
                    "summary": "Held position after review.",
                    "scratchpad_after": {
                        "current_hypotheses": [],
                        "pending_correspondence": [],
                        "standing_intent": "Hold",
                        "deferred_checks": [],
                        "notes": [{"summary": "Held position after review."}],
                    },
                    "tool_calls": [{"name": "list_messages", "arguments": {}, "output": {"ok": True}}],
                    "model_turns": 1,
                }
            ),
            log_emitter=LocalArtifactLogger(self.logs_dir),
            client_factory=lambda token: CommanderApiClient(
                base_url="http://testserver",
                token=token,
                transport=self.transport,
            ),
        )
        result = worker.run(
            commander_id=launch["commander_id"],
            run_id=launch["run_id"],
            lease_token=launch["lease_token"],
        )
        self.assertEqual(result["status"], "succeeded")

        session = create_session()
        try:
            runtime = runtime_for_commander(session, 1)
            detail = get_runtime_detail(session, 1)
            self.assertIsNone(runtime.active_run_id)
            self.assertEqual(runtime.last_run_status, "succeeded")
            self.assertEqual(detail["recent_runs"][0]["result_summary"]["summary"], "Held position after review.")
        finally:
            session.close()

        run_log_dir = self.logs_dir / "commander_runs" / "1"
        commander_log = self.logs_dir / "commanders" / "1.log"
        self.assertTrue(run_log_dir.exists())
        self.assertTrue(any(run_log_dir.iterdir()))
        self.assertTrue(commander_log.exists())

    def test_commander_dossier_specific_and_faction_fallback(self):
        session = create_session()
        try:
            dossier = load_commander_dossier(session, 1, self.config)
            self.assertEqual(dossier["source"], "commander")
            self.assertTrue(str(dossier["path"]).endswith("cmd_1_sofonisba.md"))
            self.assertIn("Queen Sofonisba", dossier["content"])

            custom_dir = Path(_TMPDIR.name) / "dossier_test"
            (custom_dir / "factions").mkdir(parents=True, exist_ok=True)
            (custom_dir / "factions" / "faction_delisgar.md").write_text(
                "# Delisgar Commander Template\n\nFaction fallback content.",
                encoding="utf-8",
            )
            fallback_config = RuntimeConfig(base_url="http://testserver", log_dir=self.logs_dir, dossier_dir=custom_dir)
            fallback = load_commander_dossier(session, 1, fallback_config)
            self.assertEqual(fallback["source"], "faction_template")
            self.assertIn("Faction fallback content.", fallback["content"])
        finally:
            session.close()

    def test_dev_endpoints_and_list_runs(self):
        response = self.http.post("/v1/admin/ai/runtimes/1/controller", json={"controller_type": "ai"})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.http.post("/v1/admin/ai/runtimes/1/nudge", json={"reason": "manual_nudge"})
        self.assertEqual(response.status_code, 200, response.text)
        runtimes = self.http.get("/v1/admin/ai/runtimes")
        self.assertEqual(runtimes.status_code, 200, runtimes.text)
        self.assertEqual(len(runtimes.json()["runtimes"]), 4)
        detail = self.http.get("/v1/admin/ai/runtimes/1")
        self.assertEqual(detail.status_code, 200, detail.text)
        runs = self.http.get("/v1/admin/ai/runs")
        self.assertEqual(runs.status_code, 200, runs.text)

    def test_mixed_control_all_ai_delivery_and_post_run_recheck(self):
        session = create_session()
        try:
            set_controller_type(session, 1, "ai")
            set_controller_type(session, 0, "ai")
            session.commit()
        finally:
            session.close()

        launched = []

        def fake_launcher(**kwargs):
            launched.append(kwargs)
            return None

        scheduler = CommanderHeartbeatScheduler(config=self.config, worker_launcher=fake_launcher)
        initial = scheduler.run_once()
        self.assertEqual(len(initial), 2)

        launch = next(item for item in launched if item["commander_id"] == 0)
        worker = CommanderWorker(
            config=self.config,
            turn_runner=SimpleNamespace(
                run_turn=lambda **kwargs: {
                    "summary": "Sent a reply.",
                    "scratchpad_after": {
                        "current_hypotheses": [],
                        "pending_correspondence": [],
                        "standing_intent": "",
                        "deferred_checks": [],
                        "notes": [],
                    },
                    "tool_calls": [
                        {
                            "name": "send_message",
                            "arguments": {"recipient_id": "cmd_1", "content": "Advance carefully."},
                            "output": self._make_client("Soolabab").send_message("cmd_1", "Advance carefully."),
                        }
                    ],
                    "model_turns": 1,
                }
            ),
            client_factory=lambda token: CommanderApiClient(
                base_url="http://testserver",
                token=token,
                transport=self.transport,
            ),
        )
        worker.run(commander_id=launch["commander_id"], run_id=launch["run_id"], lease_token=launch["lease_token"])
        self._advance_time(3)
        follow_up = scheduler.run_once()
        self.assertTrue(follow_up)


if __name__ == "__main__":
    unittest.main()
