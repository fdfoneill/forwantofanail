from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from forwantofanail.core.scenario import (
    ScenarioConfigurationError,
    clear_scenario_cache,
    load_scenario_package,
    scenario_root,
)


def _package(root: Path) -> Path:
    root.mkdir()
    csvs = {
        "terrain_types": ("terrain_types.csv", "terrain_id,terrain_name\n1,Plain\n"),
        "locations": ("locations.csv", "location_id,terrain_id\na,1\n"),
        "commanders": ("commanders.csv", "commander_id,commander_name\n1,A\n"),
        "armies": ("armies.csv", "army_id,army_name\n1,Host\n"),
        "detachments": ("detachments.csv", "detachment_id,detachment_name\n1,Foot\n"),
        "detachment_specials": ("detachment_specials.csv", "detachment_id,special_name\n1,x\n"),
        "commander_traits": ("commander_traits.csv", "commander_id,trait_name\n1,x\n"),
        "strongholds": ("strongholds.csv", "stronghold_id,stronghold_name\n1,Keep\n"),
    }
    for filename, text in csvs.values():
        (root / filename).write_text(text)
    for filename in ("templates.json", "commanders.json", "factions.json", "dossiers.json", "profiles.json", "atlas.json", "history.json"):
        (root / filename).write_text("{}")
    (root / "rules.md").write_text("rules")
    (root / "map.png").write_bytes(b"png")
    (root / "portraits").mkdir()
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        (root / f"points{suffix}").write_bytes(b"x")
    manifest = {
        "schema_version": 1, "scenario_id": "test", "scenario_version": "7",
        "title": "Test", "calendar_start_date": "1410-05-20",
        "csv_files": {key: value[0] for key, value in csvs.items()},
        "army_management_templates": "templates.json", "commander_overviews": "commanders.json",
        "faction_overviews": "factions.json", "agent_rules": "rules.md",
        "agent_commander_dossiers": "dossiers.json", "agent_profiles": "profiles.json",
        "agent_strategic_atlas": "atlas.json", "history_export_config": "history.json",
        "display_map": "map.png", "portraits_dir": "portraits", "stronghold_points": "points.shp",
    }
    (root / "scenario_manifest.json").write_text(json.dumps(manifest))
    return root


def test_explicit_scenario_precedes_environment_and_reads_calendar(tmp_path, monkeypatch):
    chosen = _package(tmp_path / "chosen")
    monkeypatch.setenv("SCENARIO_DIR", str(tmp_path / "wrong"))
    package = load_scenario_package(chosen)
    assert package.scenario_id == "test"
    assert package.calendar_start_date.isoformat() == "1410-05-20"
    assert len(package.database_source_fingerprint) == 64


def test_scenario_dir_is_required_and_absolute(monkeypatch):
    monkeypatch.delenv("SCENARIO_DIR", raising=False)
    clear_scenario_cache()
    with pytest.raises(ScenarioConfigurationError, match="SCENARIO_DIR is required"):
        scenario_root()
    monkeypatch.setenv("SCENARIO_DIR", "relative/package")
    with pytest.raises(ScenarioConfigurationError, match="absolute"):
        scenario_root()


def test_manifest_paths_cannot_escape_package(tmp_path):
    root = _package(tmp_path / "scenario")
    manifest_path = root / "scenario_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["display_map"] = "../outside.png"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ScenarioConfigurationError, match="escapes"):
        load_scenario_package(root)


def test_repository_tracks_core_terrain_but_not_scenario_or_runtime_data():
    repository = Path(__file__).resolve().parents[2]
    if not (repository / ".git").exists():
        pytest.skip("repository policy check requires a Git checkout")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    assert not any(path.startswith("forwantofanail/data/") for path in tracked)
    assert not any(path.startswith("logs/") for path in tracked)
    assert any(path.startswith("forwantofanail/web/static/terrain/") for path in tracked)

    production = repository / "forwantofanail"
    offenders = []
    for path in production.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if 'parents[1] / "data"' in text or "forwantofanail/data" in text:
            offenders.append(str(path.relative_to(repository)))
    assert offenders == []
