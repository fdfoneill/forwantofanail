from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from forwantofanail.core.database import create_session
from forwantofanail.core.models import AgentRun, AgentRunEvent

from .context import load_profiles
from .providers import adapter_for


RUBRIC = {
    "destination_grounding": "Strategic destinations are named and supported by atlas or route observations.",
    "dossier_alignment": "The plan advances the commander's authored goals and respects boundaries.",
    "atlas_tool_use": "Strategic claims follow successful atlas and route tool calls.",
    "plan_coherence": "Campaign objective, operational objective, next step, and orders agree.",
    "unsupported_invention": "The commander avoids inventing geography, intelligence, or completed actions.",
    "passivity_recovery": "The commander responds constructively to stagnation or review requirements.",
}


def _transcript(session, run: AgentRun) -> list[dict[str, Any]]:
    rows = session.query(AgentRunEvent).filter(AgentRunEvent.run_id == run.run_id).order_by(AgentRunEvent.sequence).all()
    return [{"sequence": row.sequence, "kind": row.event_kind, "payload": json.loads(row.payload_json)} for row in rows]


def evaluate(run_id: int, profile_id: str) -> dict[str, Any]:
    profiles = load_profiles()
    profile = profiles.get(profile_id)
    if profile is None or not profile.available:
        raise ValueError(f"Evaluation profile {profile_id!r} is unavailable")
    session = create_session()
    try:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"Agent run {run_id} does not exist")
        transcript = _transcript(session, run)
        source_usage = {"input_tokens": int(run.input_tokens or 0), "output_tokens": int(run.output_tokens or 0)}
    finally:
        session.close()
    prompt = (
        "Evaluate this fictional strategy-game agent transcript. Return JSON only with a `scores` object whose keys "
        "exactly match the rubric and integer values from 1 (poor) to 5 (excellent), plus concise `findings` and "
        "`recommended_prompt_changes` arrays. Judge unsupported invention inversely: 5 means no unsupported invention.\n\n"
        f"RUBRIC\n{json.dumps(RUBRIC, ensure_ascii=False, indent=2)}\n\nTRANSCRIPT\n"
        f"{json.dumps(transcript, ensure_ascii=False)}"
    )
    turn = adapter_for(profile).invoke(
        [{"role": "system", "content": "You are a rigorous, concise evaluator. Output valid JSON only."}, {"role": "user", "content": prompt}],
        [], profile,
    )
    try:
        judgment = json.loads(turn.content)
    except ValueError as exc:
        raise ValueError("Evaluation provider did not return valid JSON") from exc
    scores = judgment.get("scores") if isinstance(judgment, dict) else None
    if not isinstance(scores, dict) or set(scores) != set(RUBRIC) or any(not isinstance(value, int) or not 1 <= value <= 5 for value in scores.values()):
        raise ValueError("Evaluation provider returned an invalid rubric score set")
    return {
        "run_id": run_id, "evaluation_profile": profile_id, "provider": profile.provider, "model": profile.model,
        "scores": scores, "findings": judgment.get("findings", []),
        "recommended_prompt_changes": judgment.get("recommended_prompt_changes", []),
        "usage": {
            "source_run": source_usage,
            "evaluation": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Optionally evaluate one completed agent heartbeat with OpenAI or Ollama.")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--profile", required=True, help="Configured OpenAI or Ollama profile ID used as evaluator.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.run_id, args.profile)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
