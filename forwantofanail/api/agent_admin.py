from __future__ import annotations

import json
import os
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from forwantofanail.api.routes import (
    _get_session, _parse_commander_ref, _run_idempotent_mutation, _validate_admin_token,
)
from forwantofanail.agent_runtime.context import load_profiles
from forwantofanail.agent_runtime.service import (
    admin_overview, assign_agent, cancel_and_requeue_run, disable_agent, replace_memory, retry_run, serialize_run, skip_run,
)
from forwantofanail.core.models import (
    AgentAssignment, AgentCommanderDossier, AgentMemoryRevision, AgentRun, AgentRunEvent, GameClock,
)


router = APIRouter(prefix="/v1/admin/agents", tags=["agent-commanders"])


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=80)


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=12000)


class ProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=80)


def _admin(x_admin_token: str | None = Header(default=None)) -> None:
    _validate_admin_token(x_admin_token)


@router.get("")
def get_agents(_: None = Depends(_admin), session: Session = Depends(_get_session)):
    return admin_overview(session)


@router.post("/{commander_id}/assign")
def post_assign(
    commander_id: str, payload: AssignRequest, _: None = Depends(_admin),
    session: Session = Depends(_get_session), idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    commander_pk = _parse_commander_ref(commander_id)
    return _run_idempotent_mutation(
        session, actor_scope="admin", route=f"agent-assign:{commander_pk}", idempotency_key=idempotency_key,
        payload=payload,
        operation=lambda: {"assignment": {
            "commander_id": commander_id,
            "profile_id": assign_agent(session, commander_pk, payload.profile_id).profile_id,
            "enabled": True,
        }},
    )


@router.post("/{commander_id}/disable")
def post_disable(
    commander_id: str, _: None = Depends(_admin), session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    commander_pk = _parse_commander_ref(commander_id)
    return _run_idempotent_mutation(
        session, actor_scope="admin", route=f"agent-disable:{commander_pk}", idempotency_key=idempotency_key,
        payload={"commander_id": commander_id},
        operation=lambda: {"commander_id": commander_id, "enabled": disable_agent(session, commander_pk).enabled},
    )


@router.post("/{commander_id}/retry")
def post_retry(
    commander_id: str, _: None = Depends(_admin), session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    commander_pk = _parse_commander_ref(commander_id)
    clock = session.get(GameClock, 1)
    if clock is None:
        raise HTTPException(status_code=503, detail="Game clock is unavailable")
    return _run_idempotent_mutation(
        session, actor_scope="admin", route=f"agent-retry:{commander_pk}:{clock.world_tick}", idempotency_key=idempotency_key,
        payload={"commander_id": commander_id, "world_tick": clock.world_tick},
        operation=lambda: {"run": serialize_run(retry_run(session, commander_pk, int(clock.world_tick)))},
    )


@router.post("/{commander_id}/skip")
def post_skip(
    commander_id: str, _: None = Depends(_admin), session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    commander_pk = _parse_commander_ref(commander_id)
    clock = session.get(GameClock, 1)
    if clock is None:
        raise HTTPException(status_code=503, detail="Game clock is unavailable")
    return _run_idempotent_mutation(
        session, actor_scope="admin", route=f"agent-skip:{commander_pk}:{clock.world_tick}", idempotency_key=idempotency_key,
        payload={"commander_id": commander_id, "world_tick": clock.world_tick},
        operation=lambda: {"run": serialize_run(skip_run(session, commander_pk, int(clock.world_tick)))},
    )


@router.post("/{commander_id}/cancel")
def post_cancel(
    commander_id: str, _: None = Depends(_admin), session: Session = Depends(_get_session),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    commander_pk = _parse_commander_ref(commander_id)
    clock = session.get(GameClock, 1)
    if clock is None:
        raise HTTPException(status_code=503, detail="Game clock is unavailable")
    return _run_idempotent_mutation(
        session, actor_scope="admin", route=f"agent-cancel:{commander_pk}:{clock.world_tick}", idempotency_key=idempotency_key,
        payload={"commander_id": commander_id, "world_tick": clock.world_tick},
        operation=lambda: {"run": serialize_run(cancel_and_requeue_run(session, commander_pk, int(clock.world_tick)))},
    )


@router.get("/{commander_id}/runs")
def get_runs(
    commander_id: str, limit: int = Query(20, ge=1, le=100), _: None = Depends(_admin),
    session: Session = Depends(_get_session),
):
    commander_pk = _parse_commander_ref(commander_id)
    rows = session.query(AgentRun).filter(AgentRun.commander_id == commander_pk).order_by(AgentRun.run_id.desc()).limit(limit).all()
    return {"runs": [serialize_run(row) for row in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: int, _: None = Depends(_admin), session: Session = Depends(_get_session)):
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    events = session.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.sequence).all()
    return {
        "run": serialize_run(run),
        "events": [{
            "sequence": row.sequence, "kind": row.event_kind,
            "payload": json.loads(row.payload_json), "created_at": row.created_at,
            "duration_ms": row.duration_ms,
        } for row in events],
    }


@router.get("/{commander_id}/context")
def get_context(commander_id: str, _: None = Depends(_admin), session: Session = Depends(_get_session)):
    commander_pk = _parse_commander_ref(commander_id)
    assignment = session.get(AgentAssignment, commander_pk)
    dossier = session.get(AgentCommanderDossier, commander_pk)
    memories = session.query(AgentMemoryRevision).filter(AgentMemoryRevision.commander_id == commander_pk).order_by(AgentMemoryRevision.revision.desc()).limit(30).all()
    return {
        "dossier": None if dossier is None else {
            "source_kind": dossier.source_kind, "revision": dossier.revision,
            "content": json.loads(dossier.content_json), "content_hash": dossier.content_hash,
        },
        "memory_revision": assignment.current_memory_revision if assignment else 0,
        "memory": memories[0].content if memories else None,
        "memory_history": [{
            "revision": row.revision, "content": row.content, "author_kind": row.author_kind,
            "run_id": row.run_id, "created_at": row.created_at,
        } for row in memories],
    }


@router.put("/{commander_id}/memory")
def put_memory(
    commander_id: str, payload: MemoryRequest, _: None = Depends(_admin),
    session: Session = Depends(_get_session), idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    commander_pk = _parse_commander_ref(commander_id)
    active = session.query(AgentRun).filter(AgentRun.commander_id == commander_pk, AgentRun.status == "running").first()
    if active is not None:
        raise HTTPException(status_code=409, detail={"code": "agent_run_active", "message": "Cannot edit memory during an active heartbeat."})
    return _run_idempotent_mutation(
        session, actor_scope="admin", route=f"agent-memory:{commander_pk}", idempotency_key=idempotency_key,
        payload=payload,
        operation=lambda: {"revision": replace_memory(
            session, commander_pk, payload.expected_revision, payload.content, author_kind="admin"
        ).revision},
    )


@router.post("/providers/test")
def test_provider(payload: ProviderTestRequest, _: None = Depends(_admin)):
    profile = load_profiles().get(payload.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    if not profile.available:
        raise HTTPException(status_code=503, detail=profile.unavailable_reason)
    if profile.provider == "ollama":
        response = httpx.get(f"{os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')}/api/tags", timeout=10)
        response.raise_for_status()
        names = {row.get("name") for row in response.json().get("models", [])}
        if profile.model not in names and not any(str(name).split(":")[0] == str(profile.model).split(":")[0] for name in names):
            raise HTTPException(status_code=503, detail=f"Ollama model {profile.model!r} is not installed")
    else:
        try:
            from openai import OpenAI

            OpenAI().models.retrieve(profile.model, timeout=10)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="OpenAI provider check failed") from exc
    return {"profile_id": profile.profile_id, "provider": profile.provider, "model": profile.model, "available": True}
