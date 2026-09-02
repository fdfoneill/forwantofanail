from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from forwantofanail.core.models import AgentCommanderDossier, Commander


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
INITIAL_MEMORY = "## Current plan\n\nNo plan recorded yet.\n\n## Commitments\n\nNone recorded.\n\n## Intelligence to revisit\n\nNone recorded."


@dataclass(frozen=True)
class AgentProfile:
    profile_id: str
    label: str
    provider: str
    model_env: str
    model: str | None
    temperature: float | None
    request_timeout_seconds: int
    wall_time_seconds: int
    max_model_turns: int
    max_tool_calls: int
    max_output_tokens_per_turn: int
    max_total_output_tokens: int

    @property
    def available(self) -> bool:
        if not self.model:
            return False
        if self.provider == "openai":
            return bool(os.getenv("OPENAI_API_KEY"))
        return self.provider == "ollama"

    @property
    def unavailable_reason(self) -> str | None:
        if not self.model:
            return f"{self.model_env} is not configured"
        if self.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
            return "OPENAI_API_KEY is not configured"
        return None


def _manifest() -> dict[str, Any]:
    return json.loads((DATA_DIR / "scenario_manifest.json").read_text(encoding="utf-8"))


def _scenario_path(key: str) -> Path:
    value = _manifest().get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Scenario manifest requires {key!r}")
    path = (DATA_DIR / value).resolve()
    if DATA_DIR.resolve() not in (path, *path.parents):
        raise ValueError(f"Scenario path for {key!r} escapes the data directory")
    return path


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_rules() -> tuple[str, str]:
    value = _scenario_path("agent_rules").read_text(encoding="utf-8").strip()
    if len(value) < 500:
        raise ValueError("The canonical agent rules summary is missing or too short")
    required_topics = (
        "## Time and orders", "## Movement and reconnaissance",
        "## Armies, supply, forage, and morale", "## Combat and sieges",
        "## Correspondence and reports", "## Conduct of a heartbeat",
    )
    missing = [topic for topic in required_topics if topic not in value]
    if missing:
        raise ValueError(f"The canonical agent rules summary is missing topics: {', '.join(missing)}")
    return value, content_hash(value)


def load_dossier_source() -> dict[str, Any]:
    payload = json.loads(_scenario_path("agent_commander_dossiers").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("commanders"), dict):
        raise ValueError("Unsupported agent commander dossier schema")
    if not isinstance(payload.get("subcommander_archetypes"), list) or not payload["subcommander_archetypes"]:
        raise ValueError("Agent dossiers require subcommander archetypes")
    text_fields = {"faction", "identity", "background", "temperament", "worldview", "relationships", "voice"}
    list_fields = {"goals", "strategic_biases", "boundaries"}
    for commander_ref in ("cmd_0", "cmd_1", "cmd_2", "cmd_3"):
        row = payload["commanders"].get(commander_ref)
        if not isinstance(row, dict):
            raise ValueError(f"Missing authored dossier for {commander_ref}")
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in text_fields):
            raise ValueError(f"Authored dossier {commander_ref} has a missing text field")
        if any(not isinstance(row.get(key), list) or not row[key] for key in list_fields):
            raise ValueError(f"Authored dossier {commander_ref} has a missing list field")
    for row in payload["subcommander_archetypes"]:
        if not isinstance(row, dict) or any(not str(row.get(key) or "").strip() for key in ("temperament", "voice", "tendency")):
            raise ValueError("Invalid subcommander archetype")
    return payload


def load_faction_overview(faction: str) -> str:
    payload = json.loads((DATA_DIR / "faction_overviews.json").read_text(encoding="utf-8"))
    value = payload.get(faction)
    return str(value).strip() if value else f"No additional historical overview is recorded for {faction}."


def load_profiles() -> dict[str, AgentProfile]:
    payload = json.loads(_scenario_path("agent_profiles").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported agent profile schema")
    profiles: dict[str, AgentProfile] = {}
    for row in payload.get("profiles", []):
        profile_id = str(row["id"]).strip()
        provider = str(row["provider"]).strip().lower()
        if provider not in {"openai", "ollama"} or not profile_id or profile_id in profiles:
            raise ValueError(f"Invalid or duplicate agent profile {profile_id!r}")
        model_env = str(row["model_env"]).strip()
        raw_temperature = row.get("temperature", 0.7)
        profiles[profile_id] = AgentProfile(
            profile_id=profile_id,
            label=str(row.get("label") or profile_id),
            provider=provider,
            model_env=model_env,
            model=(os.getenv(model_env) or "").strip() or None,
            temperature=None if raw_temperature is None else float(raw_temperature),
            request_timeout_seconds=int(row.get("request_timeout_seconds", 180)),
            wall_time_seconds=int(row.get("wall_time_seconds", 600)),
            max_model_turns=int(row.get("max_model_turns", 24)),
            max_tool_calls=int(row.get("max_tool_calls", 40)),
            max_output_tokens_per_turn=int(row.get("max_output_tokens_per_turn", 4096)),
            max_total_output_tokens=int(row.get("max_total_output_tokens", 24000)),
        )
        profile = profiles[profile_id]
        if any(value <= 0 for value in (
            profile.request_timeout_seconds, profile.wall_time_seconds, profile.max_model_turns,
            profile.max_tool_calls, profile.max_output_tokens_per_turn, profile.max_total_output_tokens,
        )) or (profile.temperature is not None and not 0 <= profile.temperature <= 2):
            raise ValueError(f"Agent profile {profile_id!r} has invalid limits")
    return profiles


def _faction_for_commander(session: Session, commander_id: int) -> str:
    from forwantofanail.core.models import Army

    army = session.query(Army).filter(Army.commander_id == commander_id).first()
    return str(army.army_faction if army is not None else "Unknown")


def build_dossier(session: Session, commander: Commander) -> tuple[str, dict[str, Any]]:
    source = load_dossier_source()
    source_row = source["commanders"].get(f"cmd_{commander.commander_id}")
    if commander.created_by_commander_id is None and isinstance(source_row, dict):
        return "scenario", source_row

    creator = session.get(Commander, commander.created_by_commander_id) if commander.created_by_commander_id is not None else None
    archetypes = source["subcommander_archetypes"]
    seed = hashlib.sha256(
        f"{commander.commander_id}:{commander.commander_name}:{commander.commander_title}".encode("utf-8")
    ).digest()
    archetype = archetypes[int.from_bytes(seed[:4], "big") % len(archetypes)]
    faction = _faction_for_commander(session, commander.commander_id)
    creator_name = creator.commander_name if creator is not None else "the high command"
    appointed = (
        f"day {commander.created_day}, watch {commander.created_watch}"
        if commander.created_day is not None and commander.created_watch is not None
        else "during the present campaign"
    )
    return "generated", {
        "faction": faction,
        "identity": f"{commander.commander_title} {commander.commander_name}, a {faction} subcommander",
        "background": f"Appointed by {creator_name} {appointed} to command a newly organized field force.",
        "temperament": archetype["temperament"],
        "worldview": f"Loyal to {faction} and bound by the chain of command established by {creator_name}.",
        "goals": ["Carry out the high commander's intent", "Preserve the effectiveness of the assigned army", "Report important developments promptly"],
        "relationships": f"Subordinate to {creator_name}; other political relationships are not established.",
        "voice": archetype["voice"],
        "strategic_biases": [archetype["tendency"]],
        "boundaries": ["Do not invent independent titles, claims, or prior relationships", "Do not knowingly act against the appointing commander's strategic purpose"],
    }


def ensure_dossier(session: Session, commander: Commander) -> AgentCommanderDossier:
    existing = session.get(AgentCommanderDossier, commander.commander_id)
    if existing is not None:
        return existing
    source_kind, content = build_dossier(session, commander)
    serialized = canonical_json(content)
    now = datetime.now(timezone.utc)
    row = AgentCommanderDossier(
        commander_id=commander.commander_id,
        source_kind=source_kind,
        revision=1,
        content_json=serialized,
        content_hash=content_hash(serialized),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def dossier_as_markdown(row: AgentCommanderDossier) -> str:
    value = json.loads(row.content_json)
    lines = [f"# {value.get('identity', 'Commander')}"]
    for key in ("background", "temperament", "worldview", "relationships", "voice"):
        if value.get(key):
            lines.extend(("", f"## {key.replace('_', ' ').title()}", "", str(value[key])))
    for key in ("goals", "strategic_biases", "boundaries"):
        if value.get(key):
            lines.extend(("", f"## {key.replace('_', ' ').title()}", ""))
            lines.extend(f"- {item}" for item in value[key])
    return "\n".join(lines)
