from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forwantofanail.agent_runtime.context import ensure_dossier, load_profiles, load_rules
from forwantofanail.agent_runtime.service import (
    append_event, assign_agent, authenticate_run_session, barrier_state, disable_agent,
    enforce_advance_barrier, issue_run_session, replace_memory, skip_run,
)
from forwantofanail.agent_runtime.providers import (
    ModelToolCall,
    ModelTurn,
    OllamaAdapter,
    OpenAIAdapter,
    function_tools,
    openai_function_tools,
)
from forwantofanail.agent_runtime import worker
from forwantofanail.agent_tools.registry import catalog as gameplay_catalog
from forwantofanail.api.app import app
from forwantofanail.core.database import Base, create_session, get_engine, reset_database_runtime
from forwantofanail.core.models import (
    AgentAssignment, AgentMemoryRevision, AgentRun, Army, AuthToken, Commander, CommanderClaim,
    Detachment, GameClock, Location, TerrainType,
)


@pytest.fixture()
def agent_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'agents.db'}")
    monkeypatch.setenv("ADMIN_TOKEN", "agent-admin")
    monkeypatch.setenv("SESSION_SECRET", "agent-secret")
    monkeypatch.setenv("OLLAMA_AGENT_MODEL", "test-model")
    reset_database_runtime()
    Base.metadata.create_all(get_engine())
    session = create_session()
    session.add(TerrainType(terrain_id=1, terrain_name="Open Ground", speed_multiplier=1, scout_multiplier=1, is_water=False))
    session.add(Location(location_id="8f6889082cc8281", terrain_id=1, is_road=True, settlement=2))
    session.add_all([
        Commander(commander_id=0, commander_name="Soolabab", commander_age=48, commander_title="High Marshall"),
        Commander(commander_id=7, commander_name="Tarin", commander_age=30, commander_title="Captain", created_by_commander_id=0, created_day=2, created_watch=3),
    ])
    session.flush()
    session.add_all([
        Army(army_id=1, location_id="8f6889082cc8281", army_name="The Levy", army_faction="Boonan", commander_id=0, army_supply=100, army_morale=9, army_resting_morale=9, is_garrison=False),
        Army(army_id=2, location_id="8f6889082cc8281", army_name="The Second Levy", army_faction="Boonan", commander_id=7, army_supply=100, army_morale=9, army_resting_morale=9, is_garrison=False),
    ])
    session.add_all([
        Detachment(detachment_id=1, detachment_name="Levy", army_id=1, warrior_count=100),
        Detachment(detachment_id=2, detachment_name="Second Levy", army_id=2, warrior_count=100),
    ])
    session.add(GameClock(singleton_id=1, day=1, watch=1, world_tick=0))
    session.commit()
    session.close()
    yield
    reset_database_runtime()


def test_rules_profiles_and_dossiers_are_stable(agent_db):
    rules, digest = load_rules()
    assert "## Combat and sieges" in rules
    assert len(digest) == 64
    assert load_profiles()["ollama_default"].available is True
    assert load_profiles()["ollama_default"].temperature == 0.2
    assert load_profiles()["openai_default"].temperature is None
    session = create_session()
    original = ensure_dossier(session, session.get(Commander, 0))
    generated = ensure_dossier(session, session.get(Commander, 7))
    session.commit()
    assert original.source_kind == "scenario"
    assert json.loads(original.content_json)["identity"].startswith("High Marshall Soolabab")
    assert generated.source_kind == "generated"
    first = generated.content_json
    session.expunge(generated)
    assert ensure_dossier(session, session.get(Commander, 7)).content_json == first
    session.close()


def test_assignment_memory_barrier_and_disable(agent_db):
    session = create_session()
    assignment = assign_agent(session, 0, "ollama_default")
    session.commit()
    assert assignment.current_memory_revision == 1
    assert barrier_state(session, 0)["pending"][0]["status"] == "queued"
    with pytest.raises(HTTPException) as exc:
        enforce_advance_barrier(session, 0)
    assert exc.value.status_code == 409
    skip_run(session, 0, 0)
    session.commit()
    assert barrier_state(session, 0)["can_advance"] is True
    memory = replace_memory(session, 0, 1, "## Current plan\n\nHold the road.", author_kind="admin")
    session.commit()
    assert memory.revision == 2
    disable_agent(session, 0)
    session.commit()
    assert session.get(AgentAssignment, 0).enabled is False
    session.close()


def test_human_claim_and_agent_assignment_are_mutually_exclusive(agent_db):
    session = create_session()
    now = datetime.now(timezone.utc)
    session.add(AuthToken(token="human-token", commander_id=0, created_at=now, last_used_at=now, client_kind="api"))
    session.add(CommanderClaim(commander_id=0, token="human-token", claimed_at=now))
    session.commit()
    with pytest.raises(HTTPException) as exc:
        assign_agent(session, 0, "ollama_default")
    assert exc.value.status_code == 409
    session.rollback()
    session.close()


def test_run_credentials_are_run_and_watch_scoped(agent_db):
    session = create_session()
    assignment = assign_agent(session, 0, "ollama_default")
    session.flush()
    run = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    run.status = "running"
    raw = issue_run_session(session, run, 60)
    session.commit()
    assert authenticate_run_session(session, raw) == 0
    session.get(GameClock, 1).world_tick = 1
    session.commit()
    assert authenticate_run_session(session, raw) is None
    session.close()


def test_run_credentials_can_only_access_tool_facade(agent_db):
    session = create_session()
    assign_agent(session, 0, "ollama_default")
    session.flush()
    run = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    run.status = "running"
    raw = issue_run_session(session, run, 60)
    session.commit()
    session.close()
    with TestClient(app) as client:
        catalog_response = client.get("/v1/tools", headers={"Authorization": f"Bearer {raw}"})
        player_response = client.get("/v1/me/view", headers={"Authorization": f"Bearer {raw}"})
    assert catalog_response.status_code == 200
    assert player_response.status_code == 403


def test_admin_api_and_dashboard_agent_controls(agent_db):
    with TestClient(app) as client:
        overview = client.get("/v1/admin/agents", headers={"X-Admin-Token": "agent-admin"})
        assigned = client.post(
            "/v1/admin/agents/cmd_0/assign",
            headers={"X-Admin-Token": "agent-admin", "Idempotency-Key": "assign-0"},
            json={"profile_id": "ollama_default"},
        )
        claimable = client.get("/v1/auth/commanders")
        dashboard = client.get("/dev/dashboard")
    assert overview.status_code == 200
    assert assigned.status_code == 200
    assert assigned.json()["assignment"]["enabled"] is True
    assert "cmd_0" not in {row["id"] for row in claimable.json()}
    assert 'id="agentList"' in dashboard.text
    assert 'id="agentModalOverlay"' in dashboard.text
    assert "Route Planner" not in dashboard.text
    assert "/v1/admin/navigation/options" not in dashboard.text


def test_worker_completes_structured_heartbeat_and_persists_memory(agent_db, monkeypatch):
    session = create_session()
    assign_agent(session, 0, "ollama_default")
    session.commit()
    run = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    run.status = "running"
    run.provider = "ollama"
    run.model = "test-model"
    run.started_at = datetime.now(timezone.utc)
    assignment = session.get(AgentAssignment, 0)
    assignment.strategic_review_required = False
    session.get(AgentMemoryRevision, (0, 1)).strategic_plan_json = json.dumps({
        "campaign_objective": "Hold the frontier", "operational_objective": "Secure the road",
        "posture": "defend", "rationale": "The dossier requires defense", "immediate_next_step": "Observe",
        "assumptions": [], "reconsideration_conditions": ["Enemy movement"], "review_interval_watches": 5,
    })
    session.commit()
    run_id = run.run_id
    session.close()
    class FakeAdapter:
        def __init__(self):
            self.turn = 0

        def invoke(self, messages, tools, profile):
            self.turn += 1
            if self.turn == 1:
                return ModelTurn(tool_calls=[ModelToolCall(
                    "memory-1", "fwoan_update_scratchpad",
                    {"expected_revision": 1, "content": "## Current plan\n\nHold the road and await reports."},
                )], input_tokens=100, output_tokens=20)
            return ModelTurn(tool_calls=[ModelToolCall(
                "finish-1", "fwoan_finish_heartbeat",
                {"assessment": "The road is secure.", "actions_taken": [], "unresolved_matters": ["Await reports"], "next_intent": "Reassess next watch."},
            )], input_tokens=80, output_tokens=15)

    monkeypatch.setattr(worker, "adapter_for", lambda _profile: FakeAdapter())
    monkeypatch.setattr(worker, "_invoke_gameplay_tool", lambda *_args, **_kwargs: {
        "ok": True, "result": {"tool": "fwoan_get_situation", "as_of": "May 20, Matin watch", "data": {"brief": "All is quiet."}}
    })
    worker.execute_run(run_id, "run-token")

    session = create_session()
    finished = session.get(AgentRun, run_id)
    assignment = session.get(AgentAssignment, 0)
    assert finished.status == "completed"
    assert finished.model_turns == 2
    assert finished.tool_calls == 2
    assert assignment.current_memory_revision == 2
    assert json.loads(finished.final_summary_json)["next_intent"] == "Reassess next watch."
    assert session.get(AgentMemoryRevision, (0, 2)).content.endswith("await reports.")
    session.close()


def test_required_strategic_review_consults_atlas_and_persists_plan(agent_db, monkeypatch):
    session = create_session()
    assign_agent(session, 0, "ollama_default")
    session.commit()
    run = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    run.status = "running"
    run.provider = "ollama"
    run.model = "test-model"
    run.started_at = datetime.now(timezone.utc)
    run_id = run.run_id
    session.commit()
    session.close()
    plan = {
        "campaign_objective": "Protect Boonan independence",
        "operational_objective": "Watch the southern approaches",
        "posture": "defend",
        "rationale": "The commander dossier prioritizes the Free State's defense.",
        "immediate_next_step": "Consult correspondence and observe the road.",
        "assumptions": [],
        "reconsideration_conditions": ["Enemy forces appear", "Orders arrive from high command"],
        "review_interval_watches": 5,
    }

    class ReviewingAdapter:
        turn = 0

        def invoke(self, messages, tools, profile):
            self.turn += 1
            if self.turn == 1:
                assert "STRATEGIC THEATER" in messages[1]["content"]
                assert "STRATEGIC REVIEW IS REQUIRED" in messages[1]["content"]
                return ModelTurn(tool_calls=[ModelToolCall("atlas", "fwoan_get_strategic_overview", {})])
            if self.turn == 2:
                return ModelTurn(tool_calls=[ModelToolCall("plan", "fwoan_update_scratchpad", {
                    "expected_revision": 1, "content": "Defend the southern approaches.", "strategic_plan": plan,
                })])
            return ModelTurn(tool_calls=[ModelToolCall("finish", "fwoan_finish_heartbeat", {
                "assessment": "A defensive watch is appropriate.", "actions_taken": [],
                "unresolved_matters": [], "next_intent": "Watch for enemy movement.",
            })])

    def fake_gameplay(_token, name, _arguments, _identity):
        if name == "fwoan_get_strategic_overview":
            return {"ok": True, "result": {"tool": name, "data": {
                "artifact_hash": "a" * 64, "prose": "Boonan lies south.", "major_destinations": [], "corridors": [],
            }}}
        return {"ok": True, "result": {"tool": name, "data": {"brief": "Quiet."}}}

    monkeypatch.setattr(worker, "adapter_for", lambda _profile: ReviewingAdapter())
    monkeypatch.setattr(worker, "_invoke_gameplay_tool", fake_gameplay)
    worker.execute_run(run_id, "run-token")
    session = create_session()
    finished = session.get(AgentRun, run_id)
    assignment = session.get(AgentAssignment, 0)
    memory = session.get(AgentMemoryRevision, (0, 2))
    assert finished.status == "completed"
    assert assignment.strategic_review_required is False
    assert assignment.plan_review_due_tick == 5
    assert json.loads(memory.strategic_plan_json)["posture"] == "defend"
    event_kinds = {row.event_kind for row in finished.events}
    assert {"atlas_loaded", "strategic_plan_revision", "passive_counter_changed"} <= event_kinds
    session.close()


def test_fifth_passive_watch_requires_review_and_hold_does_not_evade_it(agent_db):
    session = create_session()
    assignment = assign_agent(session, 0, "ollama_default")
    memory = session.get(AgentMemoryRevision, (0, 1))
    memory.strategic_plan_json = json.dumps({
        "campaign_objective": "Defend Boonan", "operational_objective": "Hold the road",
        "posture": "defend", "rationale": "Guard the frontier", "immediate_next_step": "Observe",
        "assumptions": [], "reconsideration_conditions": ["Enemy sighted"], "review_interval_watches": 5,
    })
    assignment.strategic_review_required = False
    first = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    now = datetime.now(timezone.utc)
    runs = [first]
    for tick in range(1, 5):
        row = AgentRun(
            commander_id=0, world_tick=tick, attempt=1, trigger="watch", status="running",
            profile_id="ollama_default", starting_memory_revision=1, created_at=now,
        )
        session.add(row)
        session.flush()
        runs.append(row)
    for row in runs:
        row.status = "running"
        append_event(session, row, "tool_call", {
            "identity": f"hold-{row.world_tick}", "call_id": "hold", "name": "fwoan_submit_order",
            "arguments": {"order": {"kind": "hold"}},
        })
        append_event(session, row, "tool_result", {
            "identity": f"hold-{row.world_tick}", "call_id": "hold", "name": "fwoan_submit_order",
            "result": {"ok": True},
        })
        session.commit()
        result, finished = worker._runtime_call(row.run_id, ModelToolCall("finish", "fwoan_finish_heartbeat", {
            "assessment": "Holding.", "actions_taken": ["Held position"],
            "unresolved_matters": [], "next_intent": "Continue observing.",
        }))
        assert result["ok"] is True and finished is True
        session.expire_all()
        assert assignment.consecutive_passive_watches == int(row.world_tick) + 1
        assert assignment.strategic_review_required is (row.world_tick == 4)
    assert assignment.strategic_review_reason == "five_passive_watches"
    session.close()


def test_plan_destination_reference_round_trips_and_bad_reference_is_recoverable(agent_db):
    legacy = worker._diegetic_plan({
        "posture": "advance", "destination": "Brialgon", "destination_stronghold_id": 43,
    })
    assert legacy["destination_stronghold_ref"] == "sh_43"
    assert "destination_stronghold_id" not in legacy

    session = create_session()
    assignment = assign_agent(session, 0, "ollama_default")
    assignment.strategic_review_required = False
    run = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    run.status = "running"
    session.commit()
    run_id = run.run_id
    session.close()
    result, finished = worker._runtime_call(run_id, ModelToolCall("bad-plan", "fwoan_update_scratchpad", {
        "expected_revision": 1,
        "content": "Advance toward Brialgon.",
        "strategic_plan": {
            "campaign_objective": "Win the campaign",
            "operational_objective": "Advance toward Brialgon",
            "posture": "advance",
            "destination_stronghold_ref": "sh_bri algon",
            "rationale": "Brialgon is strategically important.",
            "immediate_next_step": "Review the route.",
            "assumptions": [],
            "reconsideration_conditions": ["The route becomes blocked"],
            "review_interval_watches": 2,
        },
    }))
    assert finished is False
    assert result["ok"] is False
    assert result["error"] == "invalid_destination"
    assert result["refresh_with"] == "fwoan_get_strategic_overview"
    session = create_session()
    assert session.get(AgentRun, run_id).status == "running"
    assert session.get(AgentAssignment, 0).current_memory_revision == 1
    session.close()


def test_worker_executes_only_one_tool_call_per_model_turn(agent_db, monkeypatch):
    session = create_session()
    assign_agent(session, 0, "ollama_default")
    session.commit()
    run = session.query(AgentRun).filter(AgentRun.commander_id == 0).one()
    run.status = "running"
    run.provider = "ollama"
    run.model = "test-model"
    run.started_at = datetime.now(timezone.utc)
    assignment = session.get(AgentAssignment, 0)
    assignment.strategic_review_required = False
    session.get(AgentMemoryRevision, (0, 1)).strategic_plan_json = json.dumps({
        "campaign_objective": "Hold the frontier", "operational_objective": "Choose a destination",
        "posture": "reconnoiter", "rationale": "Strategic uncertainty", "immediate_next_step": "Observe",
        "assumptions": [], "reconsideration_conditions": ["New intelligence"], "review_interval_watches": 5,
    })
    session.commit()
    run_id = run.run_id
    session.close()

    class BatchedAdapter:
        def __init__(self):
            self.turn = 0

        def invoke(self, messages, tools, profile):
            self.turn += 1
            if self.turn == 1:
                assert "Call exactly one tool per response" in messages[0]["content"]
                assert "explicitly says ok=true" in messages[0]["content"]
                return ModelTurn(tool_calls=[
                    ModelToolCall("observe", "fwoan_get_order_options", {}),
                    ModelToolCall("premature", "fwoan_submit_order", {
                        "state_token": "invented",
                        "order": {"kind": "march", "steps": ["invented"]},
                    }),
                ])
            assert any(
                message.get("role") == "user" and "Only the first tool call" in message.get("content", "")
                for message in messages
            )
            return ModelTurn(tool_calls=[ModelToolCall(
                "finish", "fwoan_finish_heartbeat",
                {
                    "assessment": "Options were reviewed; no order was issued.",
                    "actions_taken": [],
                    "unresolved_matters": ["Choose a destination"],
                    "next_intent": "Choose a destination next watch.",
                },
            )])

    invoked = []

    def fake_gameplay(_token, name, _arguments, _identity):
        invoked.append(name)
        if name == "fwoan_get_situation":
            return {"ok": True, "result": {"tool": name, "as_of": "May 20, Matin watch", "data": {"brief": "Quiet."}}}
        return {"ok": True, "result": {"tool": name, "data": {}}}

    monkeypatch.setattr(worker, "adapter_for", lambda _profile: BatchedAdapter())
    monkeypatch.setattr(worker, "_invoke_gameplay_tool", fake_gameplay)
    worker.execute_run(run_id, "run-token")

    session = create_session()
    finished = session.get(AgentRun, run_id)
    events = session.query(worker.AgentRunEvent).filter(worker.AgentRunEvent.run_id == run_id).order_by(worker.AgentRunEvent.sequence).all()
    assert finished.status == "completed"
    assert finished.tool_calls == 2
    assert invoked.count("fwoan_get_order_options") == 1
    assert "fwoan_submit_order" not in invoked
    ignored = next(event for event in events if event.event_kind == "tool_calls_ignored")
    assert json.loads(ignored.payload_json)["calls"][0]["name"] == "fwoan_submit_order"
    session.close()


def test_clock_waits_for_agents_and_force_advance_queues_next_watch(agent_db):
    with TestClient(app) as client:
        assigned = client.post(
            "/v1/admin/agents/cmd_0/assign",
            headers={"X-Admin-Token": "agent-admin", "Idempotency-Key": "clock-assign"},
            json={"profile_id": "ollama_default"},
        )
        blocked = client.post(
            "/v1/admin/time/advance",
            headers={"X-Admin-Token": "agent-admin", "Idempotency-Key": "clock-blocked"},
            json={"steps": 1, "execute_actions": False},
        )
        advanced = client.post(
            "/v1/admin/time/advance",
            headers={"X-Admin-Token": "agent-admin", "Idempotency-Key": "clock-forced"},
            json={"steps": 1, "execute_actions": False, "skip_agent_heartbeats": True},
        )
        overview = client.get("/v1/admin/agents", headers={"X-Admin-Token": "agent-admin"})
    assert assigned.status_code == 200
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "agent_runs_pending"
    assert advanced.status_code == 200
    assert overview.json()["world_tick"] == 1
    commander = next(row for row in overview.json()["commanders"] if row["commander_id"] == "cmd_0")
    assert commander["run"]["status"] == "queued"


def test_provider_adapters_normalize_visible_outputs_without_thinking(agent_db):
    profile = load_profiles()["ollama_default"]

    class Object:
        def __init__(self, **values):
            self.__dict__.update(values)

    openai_request = {}

    def create_response(**kwargs):
        openai_request.update(kwargs)
        return Object(
            output=[Object(type="function_call", call_id="call-1", name="fwoan_get_situation", arguments="{}")],
            output_text="Visible planning note", status="completed",
            usage=Object(input_tokens=12, output_tokens=7),
        )

    openai_adapter = OpenAIAdapter.__new__(OpenAIAdapter)
    openai_adapter.client = Object(responses=Object(create=create_response))
    openai_profile = replace(profile, provider="openai", temperature=None)
    openai_turn = openai_adapter.invoke([{"role": "user", "content": "Act"}], [], openai_profile)
    assert openai_turn.content == "Visible planning note"
    assert openai_turn.tool_calls[0].name == "fwoan_get_situation"
    assert "temperature" not in openai_request

    ollama_request = {}

    def create_ollama_response(**kwargs):
        ollama_request.update(kwargs)
        return Object(
            message=Object(
                content="Visible answer", thinking="private reasoning",
                tool_calls=[Object(function=Object(name="fwoan_get_situation", arguments={}))],
            ),
            prompt_eval_count=15, eval_count=9, done_reason="stop",
        )

    ollama_adapter = OllamaAdapter.__new__(OllamaAdapter)
    ollama_adapter.client = Object(chat=create_ollama_response)
    ollama_turn = ollama_adapter.invoke([{"role": "user", "content": "Act"}], [], profile)
    assert ollama_turn.content == "Visible answer"
    assert not hasattr(ollama_turn, "thinking")
    assert ollama_turn.input_tokens == 15
    assert ollama_request["options"]["temperature"] == 0.2


def test_openai_tool_schema_compiler_is_strict_and_does_not_change_ollama_schema(agent_db):
    canonical_tools = list(gameplay_catalog()["tools"]) + worker.RUNTIME_TOOLS
    canonical_snapshot = json.loads(json.dumps(canonical_tools))
    ollama_tools = function_tools(canonical_tools)
    openai_tools = openai_function_tools(canonical_tools)

    assert canonical_tools == canonical_snapshot
    assert ollama_tools[1]["function"]["parameters"] == canonical_tools[1]["input_schema"]
    canonical_submit = next(row for row in canonical_tools if row["name"] == "fwoan_submit_order")["input_schema"]
    assert "oneOf" in canonical_submit["properties"]["order"]
    assert "discriminator" in canonical_submit["properties"]["order"]

    def assert_strict_schema(value):
        if isinstance(value, list):
            for item in value:
                assert_strict_schema(item)
            return
        if not isinstance(value, dict):
            return
        assert "default" not in value
        assert "discriminator" not in value
        assert "oneOf" not in value
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert value.get("additionalProperties") is False
            assert value.get("required") == list(properties)
        for item in value.values():
            assert_strict_schema(item)

    for tool in openai_tools:
        assert tool["strict"] is True
        assert_strict_schema(tool["parameters"])

    activity = next(row for row in openai_tools if row["name"] == "fwoan_list_activity")["parameters"]
    assert "cursor" in activity["required"]
    assert any(option.get("type") == "null" for option in activity["properties"]["cursor"]["anyOf"])
    assert 'use "older"' in activity["properties"]["direction"]["description"]

    submit = next(row for row in openai_tools if row["name"] == "fwoan_submit_order")["parameters"]
    assert "anyOf" in submit["properties"]["order"]
    assert "oneOf" not in submit["properties"]["order"]
