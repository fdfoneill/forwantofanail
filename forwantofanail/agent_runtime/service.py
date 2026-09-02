from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from forwantofanail.core.models import (
    AgentAssignment,
    AgentMemoryRevision,
    AgentRun,
    AgentRunEvent,
    AgentRunSession,
    AgentWorkerHeartbeat,
    Army,
    Commander,
    CommanderClaim,
    GameClock,
)
from .context import INITIAL_MEMORY, ensure_dossier, load_profiles


ACTIVE_RUN_STATUSES = {"queued", "running"}
READY_RUN_STATUSES = {"completed", "skipped"}
FAILED_RUN_STATUSES = {"failed", "timed_out"}
TERMINAL_RUN_STATUSES = READY_RUN_STATUSES | FAILED_RUN_STATUSES | {"obsolete"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _latest_run(session: Session, commander_id: int, world_tick: int) -> AgentRun | None:
    return (
        session.query(AgentRun)
        .filter(AgentRun.commander_id == commander_id, AgentRun.world_tick == world_tick)
        .order_by(AgentRun.attempt.desc())
        .first()
    )


def append_event(session: Session, run: AgentRun, event_kind: str, payload: dict[str, Any], duration_ms: int | None = None) -> AgentRunEvent:
    sequence = int(
        session.query(func.coalesce(func.max(AgentRunEvent.sequence), 0))
        .filter(AgentRunEvent.run_id == run.run_id)
        .scalar()
        or 0
    ) + 1
    event = AgentRunEvent(
        run_id=run.run_id,
        sequence=sequence,
        event_kind=event_kind,
        payload_json=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        created_at=utcnow(),
        duration_ms=duration_ms,
    )
    session.add(event)
    session.flush()
    return event


def enqueue_run(session: Session, assignment: AgentAssignment, world_tick: int, trigger: str, *, force_attempt: bool = False) -> AgentRun | None:
    if not assignment.enabled:
        return None
    army = session.query(Army).filter(Army.commander_id == assignment.commander_id, Army.is_garrison.is_(False)).first()
    latest = _latest_run(session, assignment.commander_id, world_tick)
    if latest is not None and not force_attempt:
        return latest
    attempt = (latest.attempt + 1) if latest is not None else 1
    run = AgentRun(
        commander_id=assignment.commander_id,
        world_tick=world_tick,
        attempt=attempt,
        trigger=trigger,
        status="queued" if army is not None else "skipped",
        profile_id=assignment.profile_id,
        starting_memory_revision=assignment.current_memory_revision,
        ending_memory_revision=assignment.current_memory_revision if army is None else None,
        created_at=utcnow(),
        finished_at=utcnow() if army is None else None,
        error_code="no_field_army" if army is None else None,
        error_message="Commander has no field army." if army is None else None,
    )
    session.add(run)
    session.flush()
    append_event(session, run, "queued" if army is not None else "skipped", {"trigger": trigger, "reason": run.error_code})
    return run


def enqueue_enabled_agents(session: Session, world_tick: int, trigger: str = "watch") -> list[AgentRun]:
    rows = session.query(AgentAssignment).filter(AgentAssignment.enabled.is_(True)).order_by(AgentAssignment.commander_id).all()
    return [run for row in rows if (run := enqueue_run(session, row, world_tick, trigger)) is not None]


def reconcile_current_tick(session: Session) -> list[AgentRun]:
    clock = session.get(GameClock, 1)
    return [] if clock is None else enqueue_enabled_agents(session, int(clock.world_tick), "reconcile")


def assign_agent(session: Session, commander_id: int, profile_id: str) -> AgentAssignment:
    profile = load_profiles().get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail={"code": "profile_not_found", "message": "Unknown agent profile."})
    if not profile.available:
        raise HTTPException(status_code=503, detail={"code": "provider_unavailable", "message": profile.unavailable_reason})
    query = session.query(Commander).filter(Commander.commander_id == commander_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    commander = query.one_or_none()
    if commander is None:
        raise HTTPException(status_code=404, detail="Commander not found")
    if session.get(CommanderClaim, commander_id) is not None:
        raise HTTPException(status_code=409, detail={"code": "human_claim_active", "message": "Release the human claim before assigning an agent."})
    army = session.query(Army).filter(Army.commander_id == commander_id, Army.is_garrison.is_(False)).first()
    if army is None:
        raise HTTPException(status_code=409, detail={"code": "no_field_army", "message": "Only a commander with a field army can be assigned."})
    ensure_dossier(session, commander)
    now = utcnow()
    assignment = session.get(AgentAssignment, commander_id)
    if assignment is None:
        assignment = AgentAssignment(
            commander_id=commander_id,
            profile_id=profile_id,
            enabled=True,
            current_memory_revision=0,
            consecutive_passive_watches=0,
            strategic_review_required=True,
            strategic_review_reason="new_assignment",
            plan_review_due_tick=None,
            created_at=now,
            updated_at=now,
        )
        session.add(assignment)
        session.flush()
        session.add(AgentMemoryRevision(
            commander_id=commander_id,
            revision=1,
            content=INITIAL_MEMORY,
            author_kind="system",
            run_id=None,
            created_at=now,
        ))
        assignment.current_memory_revision = 1
    else:
        was_enabled = bool(assignment.enabled)
        active = _latest_run(session, commander_id, int(session.get(GameClock, 1).world_tick))
        if active is not None and active.status == "running":
            raise HTTPException(status_code=409, detail={"code": "agent_run_active", "message": "Cannot change an assignment while its heartbeat is running."})
        assignment.profile_id = profile_id
        assignment.enabled = True
        assignment.updated_at = now
        if not was_enabled:
            assignment.strategic_review_required = True
            assignment.strategic_review_reason = "new_assignment"
        if active is not None and active.status == "queued":
            active.profile_id = profile_id
    session.flush()
    clock = session.get(GameClock, 1)
    if clock is not None:
        latest = _latest_run(session, commander_id, int(clock.world_tick))
        if latest is None or latest.status in {"obsolete", "skipped"}:
            enqueue_run(session, assignment, int(clock.world_tick), "assignment", force_attempt=latest is not None)
    return assignment


def revoke_run_sessions(session: Session, run: AgentRun) -> None:
    now = utcnow()
    for row in session.query(AgentRunSession).filter(AgentRunSession.run_id == run.run_id, AgentRunSession.revoked_at.is_(None)).all():
        row.revoked_at = now


def disable_agent(session: Session, commander_id: int) -> AgentAssignment:
    assignment = session.get(AgentAssignment, commander_id)
    if assignment is None or not assignment.enabled:
        raise HTTPException(status_code=404, detail="Enabled agent assignment not found")
    assignment.enabled = False
    assignment.updated_at = utcnow()
    for run in session.query(AgentRun).filter(AgentRun.commander_id == commander_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES)).all():
        revoke_run_sessions(session, run)
        run.status = "obsolete"
        run.finished_at = utcnow()
        run.error_code = "assignment_disabled"
        append_event(session, run, "obsolete", {"reason": "assignment_disabled"})
    return assignment


def retry_run(session: Session, commander_id: int, world_tick: int) -> AgentRun:
    assignment = session.get(AgentAssignment, commander_id)
    if assignment is None or not assignment.enabled:
        raise HTTPException(status_code=404, detail="Enabled agent assignment not found")
    latest = _latest_run(session, commander_id, world_tick)
    if latest is None or latest.status not in FAILED_RUN_STATUSES | {"skipped", "obsolete"}:
        raise HTTPException(status_code=409, detail={"code": "run_not_retryable", "message": "The current heartbeat is not retryable."})
    return enqueue_run(session, assignment, world_tick, "retry", force_attempt=True)


def cancel_and_requeue_run(session: Session, commander_id: int, world_tick: int) -> AgentRun:
    assignment = session.get(AgentAssignment, commander_id)
    latest = _latest_run(session, commander_id, world_tick)
    if assignment is None or not assignment.enabled or latest is None or latest.status not in ACTIVE_RUN_STATUSES:
        raise HTTPException(status_code=409, detail={"code": "run_not_active", "message": "There is no active heartbeat to cancel."})
    revoke_run_sessions(session, latest)
    latest.status = "obsolete"
    latest.finished_at = utcnow()
    latest.error_code = "admin_cancelled"
    append_event(session, latest, "obsolete", {"reason": "admin_cancelled"})
    return enqueue_run(session, assignment, world_tick, "retry", force_attempt=True)


def skip_run(session: Session, commander_id: int, world_tick: int, reason: str = "admin_skip") -> AgentRun:
    run = _latest_run(session, commander_id, world_tick)
    if run is None:
        raise HTTPException(status_code=404, detail="Heartbeat not found")
    if run.status in READY_RUN_STATUSES:
        return run
    revoke_run_sessions(session, run)
    run.status = "skipped"
    run.finished_at = utcnow()
    run.ending_memory_revision = run.assignment.current_memory_revision
    run.error_code = reason
    append_event(session, run, "skipped", {"reason": reason})
    return run


def barrier_state(session: Session, world_tick: int) -> dict[str, Any]:
    assignments = session.query(AgentAssignment).filter(AgentAssignment.enabled.is_(True)).order_by(AgentAssignment.commander_id).all()
    items = []
    for assignment in assignments:
        run = _latest_run(session, assignment.commander_id, world_tick)
        items.append({
            "commander_id": assignment.commander_id,
            "run_id": run.run_id if run else None,
            "status": run.status if run else "missing",
            "error_code": run.error_code if run else None,
        })
    pending = [row for row in items if row["status"] in ACTIVE_RUN_STATUSES | {"missing"}]
    needs_resolution = [row for row in items if row["status"] in FAILED_RUN_STATUSES]
    return {
        "world_tick": world_tick,
        "enabled": len(items),
        "ready": len(items) - len(pending) - len(needs_resolution),
        "pending": pending,
        "needs_resolution": needs_resolution,
        "can_advance": not pending and not needs_resolution,
        "items": items,
    }


def enforce_advance_barrier(session: Session, world_tick: int, *, override: bool = False) -> dict[str, Any]:
    reconcile_current_tick(session)
    state = barrier_state(session, world_tick)
    if state["can_advance"]:
        return state
    if override:
        for row in state["pending"] + state["needs_resolution"]:
            skip_run(session, int(row["commander_id"]), world_tick, "forced_advance")
        return barrier_state(session, world_tick)
    code = "agent_runs_pending" if state["pending"] else "agent_runs_need_resolution"
    raise HTTPException(status_code=409, detail={"code": code, "message": "Agent heartbeats must be completed or skipped before time advances.", "barrier": state})


def issue_run_session(session: Session, run: AgentRun, lease_seconds: int) -> str:
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + timedelta(seconds=lease_seconds)
    session.add(AgentRunSession(
        token_hash=token_hash(raw), run_id=run.run_id, commander_id=run.commander_id,
        created_at=now, last_used_at=now, expires_at=expires, revoked_at=None,
    ))
    run.lease_expires_at = expires
    return raw


def authenticate_run_session(session: Session, raw: str) -> int | None:
    row = session.get(AgentRunSession, token_hash(raw))
    if row is None or row.revoked_at is not None:
        return None
    now = utcnow()
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    run = session.get(AgentRun, row.run_id)
    assignment = session.get(AgentAssignment, row.commander_id)
    clock = session.get(GameClock, 1)
    if expires <= now or run is None or run.status != "running" or assignment is None or not assignment.enabled or clock is None or int(clock.world_tick) != int(run.world_tick):
        row.revoked_at = now
        session.commit()
        return None
    row.last_used_at = now
    session.commit()
    return row.commander_id


def replace_memory(
    session: Session, commander_id: int, expected_revision: int, content: str, *,
    author_kind: str, run_id: int | None = None, strategic_plan_json: str | None = None,
) -> AgentMemoryRevision:
    content = content.strip()
    if not content or len(content) > 12000:
        raise HTTPException(status_code=422, detail="Scratchpad must contain 1 to 12,000 characters")
    assignment = session.get(AgentAssignment, commander_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Agent assignment not found")
    if int(assignment.current_memory_revision) != int(expected_revision):
        raise HTTPException(status_code=409, detail={"code": "stale_memory", "message": "Scratchpad revision is stale."})
    revision = expected_revision + 1
    previous = session.get(AgentMemoryRevision, (commander_id, expected_revision))
    row = AgentMemoryRevision(
        commander_id=commander_id, revision=revision, content=content,
        strategic_plan_json=(previous.strategic_plan_json if strategic_plan_json is None and previous else strategic_plan_json),
        author_kind=author_kind, run_id=run_id, created_at=utcnow(),
    )
    session.add(row)
    assignment.current_memory_revision = revision
    assignment.updated_at = utcnow()
    return row


def serialize_run(run: AgentRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id, "commander_id": f"cmd_{run.commander_id}", "world_tick": run.world_tick,
        "attempt": run.attempt, "trigger": run.trigger, "status": run.status,
        "profile_id": run.profile_id, "provider": run.provider, "model": run.model,
        "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
        "model_turns": run.model_turns, "tool_calls": run.tool_calls,
        "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "final_summary": json.loads(run.final_summary_json) if run.final_summary_json else None,
        "error_code": run.error_code, "error_message": run.error_message,
    }


def admin_overview(session: Session) -> dict[str, Any]:
    clock = session.get(GameClock, 1)
    tick = int(clock.world_tick) if clock else 0
    profiles = load_profiles()
    claims = {row.commander_id for row in session.query(CommanderClaim).all()}
    assignments = {row.commander_id: row for row in session.query(AgentAssignment).all()}
    commanders = (
        session.query(Commander).join(Army, Army.commander_id == Commander.commander_id)
        .filter(Army.is_garrison.is_(False)).order_by(Commander.commander_id).all()
    )
    rows = []
    for commander in commanders:
        assignment = assignments.get(commander.commander_id)
        run = _latest_run(session, commander.commander_id, tick) if assignment else None
        army = session.query(Army).filter(Army.commander_id == commander.commander_id).first()
        rows.append({
            "commander_id": f"cmd_{commander.commander_id}",
            "commander_name": f"{commander.commander_title} {commander.commander_name}".strip(),
            "army_name": army.army_name if army else None,
            "control": "human" if commander.commander_id in claims else ("agent" if assignment and assignment.enabled else "unclaimed"),
            "profile_id": assignment.profile_id if assignment else None,
            "memory_revision": assignment.current_memory_revision if assignment else 0,
            "strategic_plan": _current_plan(session, assignment),
            "consecutive_passive_watches": int(assignment.consecutive_passive_watches or 0) if assignment else 0,
            "strategic_review_required": bool(assignment.strategic_review_required) if assignment else False,
            "strategic_review_reason": assignment.strategic_review_reason if assignment else None,
            "plan_review_due": _display_tick(assignment.plan_review_due_tick) if assignment else None,
            "run": serialize_run(run) if run else None,
        })
    cutoff = utcnow() - timedelta(seconds=30)
    workers = session.query(AgentWorkerHeartbeat).filter(AgentWorkerHeartbeat.last_seen_at >= cutoff).order_by(AgentWorkerHeartbeat.worker_id).all()
    return {
        "world_tick": tick,
        "profiles": [{
            "id": p.profile_id, "label": p.label, "provider": p.provider, "model": p.model,
            "available": p.available, "unavailable_reason": p.unavailable_reason,
        } for p in profiles.values()],
        "barrier": barrier_state(session, tick),
        "workers": [{"worker_id": row.worker_id, "concurrency": row.concurrency, "last_seen_at": row.last_seen_at} for row in workers],
        "commanders": rows,
    }


def _current_plan(session: Session, assignment: AgentAssignment | None) -> dict[str, Any] | None:
    if assignment is None or not assignment.current_memory_revision:
        return None
    row = session.get(AgentMemoryRevision, (assignment.commander_id, assignment.current_memory_revision))
    if row is None or not row.strategic_plan_json:
        return None
    try:
        return json.loads(row.strategic_plan_json)
    except ValueError:
        return None


def _display_tick(world_tick: int | None) -> dict[str, Any] | None:
    if world_tick is None:
        return None
    from forwantofanail.mechanics.time import from_world_tick
    day, watch = from_world_tick(int(world_tick))
    labels = {1: "Matin", 2: "Prime", 3: "Sixbell", 4: "Vesper", 0: "Night"}
    return {"world_tick": int(world_tick), "day": day, "watch": labels[int(watch)]}
