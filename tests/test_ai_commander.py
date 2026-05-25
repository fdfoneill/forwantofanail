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
    from fastapi.testclient import TestClient
    from forwantofanail.api.app import app
    from forwantofanail.core.initialize_db import initialize_database

    INTEGRATION_DEPS_AVAILABLE = True
except Exception:
    TestClient = None
    app = None
    initialize_database = None
    INTEGRATION_DEPS_AVAILABLE = False


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
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(by_name["create_action"]["parameters"]["required"], ["kind"])
        self.assertEqual(by_name["send_message"]["parameters"]["required"], ["recipient_id", "content"])
        self.assertEqual(by_name["plan_actions"]["parameters"]["required"], ["kind", "path"])

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
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(by_name["create_action"]["parameters"]["required"], ["kind"])
        self.assertEqual(by_name["send_message"]["parameters"]["required"], ["recipient_id", "content"])
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


if __name__ == "__main__":
    unittest.main()
