from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from forwantofanail.agent_runtime.strategic_atlas import generate_atlas


def _copper_coast_dir() -> Path:
    value = os.getenv("COPPER_COAST_SCENARIO_DIR")
    if not value:
        pytest.skip("Set COPPER_COAST_SCENARIO_DIR for the optional authored-scenario smoke test")
    return Path(value)


def test_checked_in_atlas_is_deterministic_and_contains_only_reviewed_majors():
    data_dir = _copper_coast_dir()
    path = data_dir / "agent_strategic_atlas.json"
    existing = json.loads(path.read_text(encoding="utf-8"))
    regenerated = generate_atlas(data_dir)
    assert regenerated == existing
    assert regenerated["source_hashes"] == existing["source_hashes"]
    assert regenerated["artifact_hash"] == existing["artifact_hash"]
    assert len(existing["choke_point_candidates"]) == 20
    assert all(row["type"].casefold() == "city" or row["role"] == "reviewed_choke_point" for row in existing["major_strongholds"])
    assert sum(row["type"].casefold() == "city" for row in existing["major_strongholds"]) == 9
    assert existing["graph"]["component_sizes"] == sorted(existing["graph"]["component_sizes"], reverse=True)


def test_public_atlas_contains_no_h3_identifiers():
    payload = json.loads((_copper_coast_dir() / "agent_strategic_atlas.json").read_text(encoding="utf-8"))
    values = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert "h3" not in str(key).casefold()
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            values.append(value)

    visit(payload)
    assert not any(re.fullmatch(r"(?i)[0-9a-f]{15}", value) for value in values)
