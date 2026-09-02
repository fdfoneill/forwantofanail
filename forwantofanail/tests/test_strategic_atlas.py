from __future__ import annotations

import json
import re
from pathlib import Path

from forwantofanail.agent_runtime.strategic_atlas import DATA_DIR, generate_atlas


def test_checked_in_atlas_is_deterministic_and_contains_only_reviewed_majors():
    path = DATA_DIR / "agent_strategic_atlas.json"
    existing = json.loads(path.read_text(encoding="utf-8"))
    regenerated = generate_atlas(DATA_DIR)
    assert regenerated == existing
    assert regenerated["source_hashes"] == existing["source_hashes"]
    assert regenerated["artifact_hash"] == existing["artifact_hash"]
    assert len(existing["choke_point_candidates"]) == 20
    assert all(row["type"].casefold() == "city" or row["role"] == "reviewed_choke_point" for row in existing["major_strongholds"])
    assert sum(row["type"].casefold() == "city" for row in existing["major_strongholds"]) == 9
    assert existing["graph"]["component_sizes"] == sorted(existing["graph"]["component_sizes"], reverse=True)


def test_public_atlas_contains_no_h3_identifiers():
    payload = json.loads((DATA_DIR / "agent_strategic_atlas.json").read_text(encoding="utf-8"))
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
