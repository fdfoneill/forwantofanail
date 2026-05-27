from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from forwantofanail.ai_commander.client import CommanderApiClient
from forwantofanail.ai_commander.models import CommanderApiError
from forwantofanail.ai_commander.tools import CommanderToolRegistry
from forwantofanail.core.database import create_session
from forwantofanail.core.models import (
    Action,
    Army,
    AuthToken,
    Commander,
    CommanderRun,
    CommanderRuntime,
    GameClock,
    Message,
)


WATCH_SORT_ORDER = {1: 0, 2: 1, 3: 2, 4: 3, 0: 4}
ACTIVE_ACTION_STATES = {"queued", "in_progress"}
RUN_ACTIVE_STATUSES = {"queued", "running"}
DEFAULT_SCRATCHPAD = {
    "current_hypotheses": [],
    "pending_correspondence": [],
    "standing_intent": "",
    "deferred_checks": [],
    "notes": [],
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def watch_sort_key(day: int, watch: int) -> tuple[int, int]:
    return (int(day), WATCH_SORT_ORDER.get(int(watch), int(watch)))


def watch_is_newer(day: int, watch: int, other_day: int | None, other_watch: int | None) -> bool:
    if other_day is None or other_watch is None:
        return True
    return watch_sort_key(day, watch) > watch_sort_key(other_day, other_watch)


def json_loads_safe(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def default_scratchpad() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_SCRATCHPAD))


def scheduler_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def slugify(value: str) -> str:
    lowered = str(value or "").strip().lower()
    pieces: list[str] = []
    last_was_sep = False
    for char in lowered:
        if char.isalnum():
            pieces.append(char)
            last_was_sep = False
        elif not last_was_sep:
            pieces.append("_")
            last_was_sep = True
    return "".join(pieces).strip("_")


@dataclass
class RuntimeConfig:
    base_url: str = "http://127.0.0.1:8000"
    poll_interval_seconds: float = 1.0
    lease_duration_seconds: int = 30
    max_tool_calls: int = 8
    max_model_turns: int = 2
    max_run_seconds: int = 30
    recent_run_summaries_limit: int = 3
    log_dir: Path = field(default_factory=lambda: Path(os.environ.get("FWOAN_LOG_DIR", "logs")))
    dossier_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[1] / "data" / "ai_commanders"
    )
    scheduler_id: str = field(default_factory=scheduler_instance_id)


class LocalArtifactLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    def write_run_artifacts(self, commander_id: int, run: CommanderRun, payload: dict[str, Any]) -> None:
        timestamp = (run.finished_at or run.started_at or run.triggered_at or utcnow()).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self.log_dir / "commander_runs" / str(commander_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        run_path = run_dir / f"{timestamp}_{run.run_id}.json"
        run_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

        timeline_dir = self.log_dir / "commanders"
        timeline_dir.mkdir(parents=True, exist_ok=True)
        timeline_path = timeline_dir / f"{commander_id}.log"
        summary = payload.get("result_summary", {}) if isinstance(payload.get("result_summary"), dict) else {}
        summary_text = str(summary.get("summary") or payload.get("status") or "").strip()
        wake_reasons = ",".join(payload.get("wake_reasons") or [])
        line = (
            f"{timestamp} run={run.run_id} status={payload.get('status')} "
            f"wake=[{wake_reasons}] summary={summary_text}\n"
        )
        with timeline_path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def commander_faction(session: Session, commander_id: int) -> str | None:
    army = session.query(Army).filter(Army.commander_id == commander_id).first()
    if army is None:
        return None
    faction = str(army.army_faction or "").strip()
    return faction or None


def dossier_candidates(config: RuntimeConfig, commander: Commander, faction: str | None) -> list[Path]:
    commander_name_slug = slugify(commander.commander_name)
    display_slug = slugify(f"{commander.commander_title} {commander.commander_name}")
    candidates = [
        config.dossier_dir / "commanders" / f"cmd_{commander.commander_id}_{commander_name_slug}.md",
        config.dossier_dir / "commanders" / f"cmd_{commander.commander_id}_{display_slug}.md",
        config.dossier_dir / "commanders" / f"{commander_name_slug}.md",
        config.dossier_dir / "commanders" / f"{display_slug}.md",
    ]
    if faction:
        candidates.append(config.dossier_dir / "factions" / f"faction_{slugify(faction)}.md")
    return candidates


def load_commander_dossier(session: Session, commander_id: int, config: RuntimeConfig) -> dict[str, Any]:
    commander = session.get(Commander, commander_id)
    if commander is None:
        raise ValueError("Commander not found")
    faction = commander_faction(session, commander_id)
    for candidate in dossier_candidates(config, commander, faction):
        if candidate.exists():
            return {
                "path": str(candidate.resolve()),
                "source": "commander" if "commanders" in candidate.parts else "faction_template",
                "content": candidate.read_text(encoding="utf-8"),
                "faction": faction,
            }
    return {
        "path": None,
        "source": "missing",
        "content": "",
        "faction": faction,
    }


class DryRunTurnRunner:
    def run_turn(
        self,
        *,
        client: CommanderApiClient,
        registry: CommanderToolRegistry,
        brief_context: dict[str, Any],
        dossier: dict[str, Any],
        scratchpad: dict[str, Any],
        recent_summaries: list[dict[str, Any]],
        wake_reasons: list[str],
        config: RuntimeConfig,
    ) -> dict[str, Any]:
        _ = client, registry, recent_summaries, config
        summary = (
            f"Dry-run heartbeat review completed for wake reasons: {', '.join(wake_reasons) or 'none'}."
        )
        updated = default_scratchpad()
        updated.update({key: value for key, value in scratchpad.items() if key in updated})
        notes = list(updated.get("notes") or [])
        overview = brief_context.get("situation_overview") or {}
        notes.append(
            {
                "timestamp": utcnow().isoformat(),
                "summary": summary,
                "location": overview.get("location_label"),
                "dossier_source": dossier.get("source"),
            }
        )
        updated["notes"] = notes[-10:]
        return {
            "summary": summary,
            "scratchpad_after": updated,
            "tool_calls": [],
            "model_turns": 0,
        }


class OpenAIResponsesTurnRunner:
    def __init__(self, model: str):
        self.model = model

    def run_turn(
        self,
        *,
        client: CommanderApiClient,
        registry: CommanderToolRegistry,
        brief_context: dict[str, Any],
        dossier: dict[str, Any],
        scratchpad: dict[str, Any],
        recent_summaries: list[dict[str, Any]],
        wake_reasons: list[str],
        config: RuntimeConfig,
    ) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenAI package is not installed for heartbeat execution") from exc

        openai_client = OpenAI()
        tool_calls: list[dict[str, Any]] = []
        prompt = (
            "You are an AI commander in For Want of a Nail.\n"
            "Your brief is authoritative current context. Use tools only when needed.\n"
            "Return concise operational reasoning through your actions and final answer.\n\n"
            f"Commander dossier source: {json.dumps(dossier.get('source'))}\n"
            f"Commander dossier path: {json.dumps(dossier.get('path'))}\n"
            f"Commander dossier markdown:\n{dossier.get('content') or ''}\n\n"
            f"Wake reasons: {json.dumps(wake_reasons)}\n"
            f"Scratchpad: {json.dumps(scratchpad, sort_keys=True)}\n"
            f"Recent run summaries: {json.dumps(recent_summaries, sort_keys=True)}\n"
            f"Commander brief: {json.dumps(brief_context, sort_keys=True)}"
        )

        started = time.monotonic()
        response = openai_client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            tools=registry.get_tools(),
        )
        model_turns = 1
        function_outputs: list[dict[str, str]] = []
        for item in getattr(response, "output", []):
            if getattr(item, "type", None) != "function_call":
                continue
            if len(tool_calls) >= config.max_tool_calls:
                raise RuntimeError("Maximum tool call limit exceeded")
            arguments = json_loads_safe(getattr(item, "arguments", "{}"), {})
            output = registry.dispatch_json(str(getattr(item, "name", "")), arguments)
            tool_calls.append(
                {
                    "name": str(getattr(item, "name", "")),
                    "arguments": arguments,
                    "call_id": str(getattr(item, "call_id", "")),
                    "output": json_loads_safe(output, {}),
                }
            )
            function_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": str(getattr(item, "call_id", "")),
                    "output": output,
                }
            )
        if time.monotonic() - started > config.max_run_seconds:
            raise TimeoutError("AI commander run exceeded maximum duration")

        final_response = response
        if function_outputs:
            if model_turns >= config.max_model_turns:
                raise RuntimeError("Maximum model turn limit exceeded")
            final_response = openai_client.responses.create(
                model=self.model,
                previous_response_id=response.id,
                input=function_outputs,
                tools=registry.get_tools(),
            )
            model_turns += 1
        summary = str(getattr(final_response, "output_text", "") or "").strip() or "AI turn completed."
        updated = default_scratchpad()
        updated.update({key: value for key, value in scratchpad.items() if key in updated})
        notes = list(updated.get("notes") or [])
        notes.append({"timestamp": utcnow().isoformat(), "summary": summary})
        updated["notes"] = notes[-10:]
        return {
            "summary": summary,
            "scratchpad_after": updated,
            "tool_calls": tool_calls,
            "model_turns": model_turns,
        }


def build_turn_runner() -> Any:
    model = str(os.environ.get("FWOAN_AI_OPENAI_MODEL") or "").strip()
    if model and os.environ.get("OPENAI_API_KEY"):
        return OpenAIResponsesTurnRunner(model=model)
    return DryRunTurnRunner()


def ensure_runtime_rows(session: Session) -> None:
    commander_ids = [int(row[0]) for row in session.query(Commander.commander_id).all()]
    existing = {
        int(row[0])
        for row in session.query(CommanderRuntime.commander_id)
        .filter(CommanderRuntime.commander_id.in_(commander_ids))
        .all()
    }
    for commander_id in commander_ids:
        if commander_id in existing:
            continue
        session.add(
            CommanderRuntime(
                commander_id=commander_id,
                controller_type="human",
                ai_enabled=False,
                attention_needed=False,
                attention_reasons_json="[]",
                scratchpad_json=json.dumps(default_scratchpad()),
            )
        )
    session.flush()


def runtime_for_commander(session: Session, commander_id: int, *, create_missing: bool = False) -> CommanderRuntime:
    if create_missing:
        ensure_runtime_rows(session)
    runtime = session.get(CommanderRuntime, commander_id)
    if runtime is None:
        raise ValueError(f"No runtime found for commander {commander_id}")
    if not runtime.scratchpad_json:
        runtime.scratchpad_json = json.dumps(default_scratchpad())
    if not runtime.attention_reasons_json:
        runtime.attention_reasons_json = "[]"
    return runtime


def serialize_runtime(runtime: CommanderRuntime) -> dict[str, Any]:
    return {
        "commander_id": runtime.commander_id,
        "controller_type": runtime.controller_type,
        "ai_enabled": bool(runtime.ai_enabled),
        "last_reviewed_day": runtime.last_reviewed_day,
        "last_reviewed_watch": runtime.last_reviewed_watch,
        "last_reviewed_message_id": runtime.last_reviewed_message_id,
        "last_reviewed_action_fingerprint": runtime.last_reviewed_action_fingerprint,
        "attention_needed": bool(runtime.attention_needed),
        "attention_reasons": json_loads_safe(runtime.attention_reasons_json, []),
        "active_run_id": runtime.active_run_id,
        "lease_token": runtime.lease_token,
        "lease_expires_at": runtime.lease_expires_at.isoformat() if runtime.lease_expires_at is not None else None,
        "scratchpad": json_loads_safe(runtime.scratchpad_json, default_scratchpad()),
        "last_run_started_at": runtime.last_run_started_at.isoformat() if runtime.last_run_started_at else None,
        "last_run_finished_at": runtime.last_run_finished_at.isoformat() if runtime.last_run_finished_at else None,
        "last_run_status": runtime.last_run_status,
    }


def serialize_run(run: CommanderRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "commander_id": run.commander_id,
        "status": run.status,
        "triggered_at": run.triggered_at.isoformat() if run.triggered_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "wake_reasons": json_loads_safe(run.wake_reasons_json, []),
        "lease_token": run.lease_token,
        "scheduler_instance_id": run.scheduler_instance_id,
        "worker_pid": run.worker_pid,
        "brief_snapshot": json_loads_safe(run.brief_snapshot_json, {}),
        "scratchpad_before": json_loads_safe(run.scratchpad_before_json, {}),
        "scratchpad_after": json_loads_safe(run.scratchpad_after_json, {}),
        "tool_calls": json_loads_safe(run.tool_calls_json, []),
        "result_summary": json_loads_safe(run.result_summary_json, {}),
        "error": json_loads_safe(run.error_json, {}),
    }


def set_controller_type(session: Session, commander_id: int, controller_type: str) -> CommanderRuntime:
    runtime = runtime_for_commander(session, commander_id, create_missing=True)
    normalized = str(controller_type or "").strip().lower()
    if normalized not in {"human", "ai", "disabled"}:
        raise ValueError("controller_type must be one of: human, ai, disabled")
    runtime.controller_type = normalized
    runtime.ai_enabled = normalized == "ai"
    if normalized != "ai":
        if runtime.active_run_id is not None:
            active_run = session.get(CommanderRun, runtime.active_run_id)
            if active_run is not None and active_run.status in RUN_ACTIVE_STATUSES:
                active_run.status = "superseded"
                active_run.finished_at = utcnow()
                active_run.error_json = json.dumps({"message": "Commander control mode changed"})
        runtime.active_run_id = None
        runtime.lease_token = None
        runtime.lease_expires_at = None
        runtime.attention_needed = False
        runtime.attention_reasons_json = "[]"
    else:
        reasons = set(json_loads_safe(runtime.attention_reasons_json, []))
        reasons.add("startup_reconcile")
        runtime.attention_needed = True
        runtime.attention_reasons_json = json.dumps(sorted(reasons))
    session.flush()
    return runtime


def mark_manual_attention(session: Session, commander_id: int, reason: str = "manual_nudge") -> CommanderRuntime:
    runtime = runtime_for_commander(session, commander_id, create_missing=True)
    reasons = set(json_loads_safe(runtime.attention_reasons_json, []))
    reasons.add(reason)
    runtime.attention_needed = True
    runtime.attention_reasons_json = json.dumps(sorted(reasons))
    session.flush()
    return runtime


def current_action_fingerprint(session: Session, commander_id: int) -> str:
    actions = (
        session.query(Action)
        .filter(Action.commander_id == commander_id, Action.state.in_(list(ACTIVE_ACTION_STATES)))
        .order_by(Action.accepted_at.asc(), Action.action_id.asc())
        .all()
    )
    current_action = None
    remaining_moves: list[str] = []
    remaining_rout: list[str] = []
    siege_target_h3: str | None = None
    for action in actions:
        params = json_loads_safe(action.parameters_json, {})
        action_payload = {
            "action_id": action.action_id,
            "kind": action.kind,
            "state": action.state,
        }
        if action.eta_day is not None and action.eta_watch is not None:
            action_payload["eta"] = {"day": action.eta_day, "watch": action.eta_watch}
        if action.kind == "move":
            destination_h3 = str(params.get("destination_h3") or "").strip()
            if destination_h3:
                action_payload["destination_h3"] = destination_h3
                remaining_moves.append(destination_h3)
        elif action.kind == "attack":
            target_h3 = str(params.get("target_h3") or "").strip()
            if target_h3:
                action_payload["target_h3"] = target_h3
        elif action.kind == "besiege":
            target_h3 = str(params.get("target_h3") or "").strip()
            if target_h3:
                action_payload["target_h3"] = target_h3
                if action.state == "in_progress":
                    siege_target_h3 = target_h3
        elif action.kind == "rout" and action.state == "in_progress":
            remaining_rout.extend(
                [str(item).strip() for item in (params.get("path") or []) if str(item).strip()]
            )
        if current_action is None and action.state == "in_progress":
            current_action = action_payload
    payload = {
        "current_action": current_action,
        "itinerary": {
            "remaining_moves": remaining_moves,
            "remaining_rout": remaining_rout,
            "siege_target_h3": siege_target_h3,
        },
    }
    return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def latest_delivered_message_id(session: Session, commander_id: int, clock: GameClock) -> int | None:
    return (
        session.query(func.max(Message.message_id))
        .filter(
            Message.recipient_id == commander_id,
            Message.status == "received",
            (
                (Message.delivery_day < clock.day)
                | ((Message.delivery_day == clock.day) & (Message.delivery_watch <= clock.watch))
            ),
        )
        .scalar()
    )


def evaluate_runtime_attention(session: Session, runtime: CommanderRuntime, clock: GameClock | None = None) -> dict[str, Any]:
    clock = clock or session.get(GameClock, 1)
    if clock is None:
        raise ValueError("Game clock is missing")
    reasons = set(json_loads_safe(runtime.attention_reasons_json, [])) if runtime.attention_needed else set()
    first_review = runtime.last_reviewed_day is None or runtime.last_reviewed_watch is None
    if first_review and runtime.controller_type == "ai":
        reasons.add("startup_reconcile")
    elif watch_is_newer(clock.day, clock.watch, runtime.last_reviewed_day, runtime.last_reviewed_watch):
        reasons.add("clock_advanced")
    latest_message_id = latest_delivered_message_id(session, runtime.commander_id, clock)
    if latest_message_id is not None and (
        runtime.last_reviewed_message_id is None or latest_message_id > int(runtime.last_reviewed_message_id)
    ):
        reasons.add("message_received")
    fingerprint = current_action_fingerprint(session, runtime.commander_id)
    if runtime.last_reviewed_action_fingerprint is None:
        if runtime.controller_type == "ai":
            reasons.add("startup_reconcile")
    elif fingerprint != runtime.last_reviewed_action_fingerprint:
        reasons.add("action_state_changed")
    return {
        "attention_needed": bool(reasons),
        "reasons": sorted(reasons),
        "clock": clock,
        "latest_message_id": latest_message_id,
        "action_fingerprint": fingerprint,
    }


def get_or_create_runtime_auth_token(session: Session, commander_id: int) -> str:
    token_row = (
        session.query(AuthToken)
        .filter(AuthToken.commander_id == commander_id)
        .order_by(AuthToken.created_at.desc())
        .first()
    )
    if token_row is not None:
        return token_row.token
    token = secrets.token_urlsafe(24)
    session.add(AuthToken(token=token, commander_id=commander_id, created_at=utcnow()))
    session.flush()
    return token


def recent_run_summaries(session: Session, commander_id: int, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.query(CommanderRun)
        .filter(CommanderRun.commander_id == commander_id, CommanderRun.finished_at.is_not(None))
        .order_by(CommanderRun.finished_at.desc(), CommanderRun.run_id.desc())
        .limit(limit)
        .all()
    )
    summaries: list[dict[str, Any]] = []
    for row in rows:
        summary = json_loads_safe(row.result_summary_json, {})
        summaries.append(
            {
                "run_id": row.run_id,
                "status": row.status,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "summary": summary,
                "wake_reasons": json_loads_safe(row.wake_reasons_json, []),
            }
        )
    return list(reversed(summaries))


class CommanderWorker:
    def __init__(
        self,
        *,
        config: RuntimeConfig | None = None,
        turn_runner: Any | None = None,
        log_emitter: LocalArtifactLogger | None = None,
        client_factory: Callable[[str], CommanderApiClient] | None = None,
    ):
        self.config = config or RuntimeConfig()
        self.turn_runner = turn_runner or build_turn_runner()
        self.log_emitter = log_emitter or LocalArtifactLogger(self.config.log_dir)
        self.client_factory = client_factory or (lambda token: CommanderApiClient(base_url=self.config.base_url, token=token))

    def run(self, *, commander_id: int, run_id: int, lease_token: str) -> dict[str, Any]:
        session = create_session()
        try:
            runtime = runtime_for_commander(session, commander_id, create_missing=True)
            run = session.get(CommanderRun, run_id)
            if run is None or run.commander_id != commander_id:
                raise ValueError("Commander run not found")
            if runtime.active_run_id != run_id or runtime.lease_token != lease_token or run.lease_token != lease_token:
                raise ValueError("Lease token mismatch for commander run")
            run.status = "running"
            run.started_at = utcnow()
            run.worker_pid = os.getpid()
            runtime.last_run_started_at = run.started_at
            runtime.last_run_status = "running"
            session.flush()

            token = get_or_create_runtime_auth_token(session, commander_id)
            commander = session.get(Commander, commander_id)
            if commander is None:
                raise ValueError("Commander not found")
            session.commit()

            client = self.client_factory(token)
            registry = CommanderToolRegistry(client)
            brief_context = client.get_brief()

            session = create_session()
            runtime = runtime_for_commander(session, commander_id, create_missing=True)
            run = session.get(CommanderRun, run_id)
            if run is None:
                raise ValueError("Commander run not found after brief fetch")
            scratchpad_before = json_loads_safe(runtime.scratchpad_json, default_scratchpad())
            wake_reasons = json_loads_safe(run.wake_reasons_json, [])
            dossier = load_commander_dossier(session, commander_id, self.config)
            run.brief_snapshot_json = json.dumps(brief_context, sort_keys=True)
            run.scratchpad_before_json = json.dumps(scratchpad_before, sort_keys=True)
            session.flush()

            started = time.monotonic()
            result = self.turn_runner.run_turn(
                client=client,
                registry=registry,
                brief_context=brief_context,
                dossier=dossier,
                scratchpad=scratchpad_before,
                recent_summaries=recent_run_summaries(
                    session,
                    commander_id,
                    self.config.recent_run_summaries_limit,
                ),
                wake_reasons=wake_reasons,
                config=self.config,
            )
            if time.monotonic() - started > self.config.max_run_seconds:
                raise TimeoutError("Commander run exceeded max duration")
            scratchpad_after = result.get("scratchpad_after") or scratchpad_before
            summary_payload = {
                "summary": str(result.get("summary") or "").strip(),
                "model_turns": int(result.get("model_turns") or 0),
            }
            run.status = "succeeded"
            run.finished_at = utcnow()
            run.scratchpad_after_json = json.dumps(scratchpad_after, sort_keys=True)
            run.tool_calls_json = json.dumps(result.get("tool_calls") or [], sort_keys=True)
            run.result_summary_json = json.dumps(summary_payload, sort_keys=True)
            run.error_json = "{}"
            runtime.scratchpad_json = json.dumps(scratchpad_after, sort_keys=True)
            evaluation = evaluate_runtime_attention(session, runtime)
            runtime.last_reviewed_day = evaluation["clock"].day
            runtime.last_reviewed_watch = evaluation["clock"].watch
            runtime.last_reviewed_message_id = evaluation["latest_message_id"]
            runtime.last_reviewed_action_fingerprint = evaluation["action_fingerprint"]
            runtime.attention_needed = False
            runtime.attention_reasons_json = "[]"
            runtime.active_run_id = None
            runtime.lease_token = None
            runtime.lease_expires_at = None
            runtime.last_run_finished_at = run.finished_at
            runtime.last_run_status = run.status
            session.commit()
            artifact_payload = {
                "status": run.status,
                "wake_reasons": wake_reasons,
                "commander_dossier": dossier,
                "brief_snapshot": brief_context,
                "scratchpad_before": scratchpad_before,
                "scratchpad_after": scratchpad_after,
                "tool_calls": result.get("tool_calls") or [],
                "result_summary": summary_payload,
            }
            self.log_emitter.write_run_artifacts(commander_id, run, artifact_payload)
            return artifact_payload
        except Exception as exc:
            session.rollback()
            recovery = create_session()
            try:
                runtime = runtime_for_commander(recovery, commander_id, create_missing=True)
                run = recovery.get(CommanderRun, run_id)
                finished_at = utcnow()
                error_payload = {
                    "message": str(exc),
                    "type": exc.__class__.__name__,
                }
                if isinstance(exc, CommanderApiError):
                    error_payload.update(exc.to_dict())
                if run is not None:
                    run.status = "timed_out" if isinstance(exc, TimeoutError) else "failed"
                    run.finished_at = finished_at
                    run.error_json = json.dumps(error_payload, sort_keys=True)
                    run.result_summary_json = json.dumps({"summary": str(exc)}, sort_keys=True)
                runtime.active_run_id = None
                runtime.lease_token = None
                runtime.lease_expires_at = None
                runtime.last_run_finished_at = finished_at
                runtime.last_run_status = "timed_out" if isinstance(exc, TimeoutError) else "failed"
                reasons = set(json_loads_safe(runtime.attention_reasons_json, []))
                reasons.add("manual_nudge")
                runtime.attention_needed = True
                runtime.attention_reasons_json = json.dumps(sorted(reasons))
                recovery.commit()
                if run is not None:
                    self.log_emitter.write_run_artifacts(
                        commander_id,
                        run,
                        {
                            "status": run.status,
                            "wake_reasons": json_loads_safe(run.wake_reasons_json, []),
                            "commander_dossier": load_commander_dossier(recovery, commander_id, self.config),
                            "brief_snapshot": json_loads_safe(run.brief_snapshot_json, {}),
                            "scratchpad_before": json_loads_safe(run.scratchpad_before_json, {}),
                            "scratchpad_after": json_loads_safe(run.scratchpad_after_json, {}),
                            "tool_calls": json_loads_safe(run.tool_calls_json, []),
                            "result_summary": json_loads_safe(run.result_summary_json, {}),
                            "error": error_payload,
                        },
                    )
            finally:
                recovery.close()
            raise
        finally:
            session.close()


def default_worker_launcher(*, commander_id: int, run_id: int, lease_token: str, config: RuntimeConfig) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-m",
        "forwantofanail.ai_commander.cli",
        "worker",
        "--commander-id",
        str(commander_id),
        "--run-id",
        str(run_id),
        "--lease-token",
        lease_token,
        "--base-url",
        config.base_url,
        "--log-dir",
        str(config.log_dir),
    ]
    return subprocess.Popen(command)


class CommanderHeartbeatScheduler:
    def __init__(
        self,
        *,
        config: RuntimeConfig | None = None,
        worker_launcher: Callable[..., Any] | None = None,
    ):
        self.config = config or RuntimeConfig()
        self.worker_launcher = worker_launcher or default_worker_launcher

    def run_once(self) -> list[int]:
        session = create_session()
        launched_runs: list[int] = []
        try:
            ensure_runtime_rows(session)
            commander_ids = [
                int(row[0])
                for row in session.query(CommanderRuntime.commander_id)
                .filter(CommanderRuntime.controller_type == "ai")
                .order_by(CommanderRuntime.commander_id.asc())
                .all()
            ]
            clock = session.get(GameClock, 1)
            if clock is None:
                raise ValueError("Game clock is missing")
            now = utcnow()
            for commander_id in commander_ids:
                runtime = runtime_for_commander(session, commander_id, create_missing=True)
                if runtime.active_run_id is not None and runtime.lease_expires_at is not None and runtime.lease_expires_at <= now:
                    active_run = session.get(CommanderRun, runtime.active_run_id)
                    if active_run is not None and active_run.status in RUN_ACTIVE_STATUSES:
                        active_run.status = "timed_out"
                        active_run.finished_at = now
                        active_run.error_json = json.dumps({"message": "Lease expired"}, sort_keys=True)
                    runtime.active_run_id = None
                    runtime.lease_token = None
                    runtime.lease_expires_at = None
                    runtime.last_run_finished_at = now
                    runtime.last_run_status = "timed_out"

                if runtime.active_run_id is not None and runtime.lease_expires_at is not None and runtime.lease_expires_at > now:
                    continue

                evaluation = evaluate_runtime_attention(session, runtime, clock=clock)
                runtime.attention_needed = bool(evaluation["attention_needed"])
                runtime.attention_reasons_json = json.dumps(evaluation["reasons"], sort_keys=True)
                if not evaluation["attention_needed"]:
                    continue

                lease_token = secrets.token_urlsafe(24)
                lease_expires_at = utcnow().replace(microsecond=0) + timedelta(seconds=self.config.lease_duration_seconds)
                run = CommanderRun(
                    commander_id=runtime.commander_id,
                    status="queued",
                    triggered_at=utcnow(),
                    wake_reasons_json=json.dumps(evaluation["reasons"], sort_keys=True),
                    lease_token=lease_token,
                    scheduler_instance_id=self.config.scheduler_id,
                    worker_pid=None,
                )
                session.add(run)
                session.flush()
                runtime.active_run_id = run.run_id
                runtime.lease_token = lease_token
                runtime.lease_expires_at = lease_expires_at
                runtime.last_run_status = "queued"
                session.commit()
                self.worker_launcher(
                    commander_id=runtime.commander_id,
                    run_id=run.run_id,
                    lease_token=lease_token,
                    config=self.config,
                )
                launched_runs.append(run.run_id)
            return launched_runs
        finally:
            session.close()

    def run_forever(self) -> None:
        while True:
            self.run_once()
            time.sleep(self.config.poll_interval_seconds)


def list_runtime_rows(session: Session) -> list[dict[str, Any]]:
    runtimes = session.query(CommanderRuntime).order_by(CommanderRuntime.commander_id.asc()).all()
    return [serialize_runtime(runtime) for runtime in runtimes]


def get_runtime_detail(session: Session, commander_id: int, run_limit: int = 10) -> dict[str, Any]:
    runtime = runtime_for_commander(session, commander_id)
    runs = (
        session.query(CommanderRun)
        .filter(CommanderRun.commander_id == commander_id)
        .order_by(CommanderRun.triggered_at.desc(), CommanderRun.run_id.desc())
        .limit(run_limit)
        .all()
    )
    return {
        "runtime": serialize_runtime(runtime),
        "recent_runs": [serialize_run(run) for run in runs],
    }


def list_runs(session: Session, statuses: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = session.query(CommanderRun)
    if statuses:
        query = query.filter(CommanderRun.status.in_(statuses))
    rows = query.order_by(CommanderRun.triggered_at.desc(), CommanderRun.run_id.desc()).limit(limit).all()
    return [serialize_run(row) for row in rows]
