from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


MANIFEST_NAME = "scenario_manifest.json"
SUPPORTED_SCHEMA_VERSIONS = {1}
REQUIRED_CSV_KEYS = {
    "terrain_types", "locations", "commanders", "armies", "detachments",
    "detachment_specials", "commander_traits", "strongholds",
}
REQUIRED_PATH_KEYS = {
    "army_management_templates", "commander_overviews", "faction_overviews",
    "agent_rules", "agent_commander_dossiers", "agent_profiles",
    "agent_strategic_atlas", "history_export_config", "display_map",
    "portraits_dir", "stronghold_points",
}


class ScenarioConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScenarioPackage:
    root: Path
    manifest: dict[str, Any]
    calendar_start_date: date
    database_source_fingerprint: str

    @property
    def scenario_id(self) -> str:
        return str(self.manifest["scenario_id"])

    @property
    def scenario_version(self) -> str:
        return str(self.manifest["scenario_version"])

    @property
    def title(self) -> str:
        return str(self.manifest["title"])

    def resolve(self, key: str) -> Path:
        value = self.manifest.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ScenarioConfigurationError(f"Scenario manifest requires {key!r}.")
        return resolve_beneath(self.root, value)

    def csv_path(self, key: str, *, optional: bool = False) -> Path | None:
        section = self.manifest.get("optional_csv_files" if optional else "csv_files", {})
        if not isinstance(section, dict):
            raise ScenarioConfigurationError("Scenario CSV mappings must be an object.")
        value = section.get(key)
        if value is None and optional:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ScenarioConfigurationError(f"Scenario manifest lacks CSV mapping {key!r}.")
        return resolve_beneath(self.root, value)


def resolve_beneath(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ScenarioConfigurationError(f"Scenario child path must be relative: {relative!r}.")
    candidate = (root / relative).resolve()
    root = root.resolve()
    if root not in (candidate, *candidate.parents):
        raise ScenarioConfigurationError(f"Scenario path escapes its package: {relative!r}.")
    return candidate


def scenario_root(explicit: Path | str | None = None) -> Path:
    raw = explicit if explicit is not None else os.getenv("SCENARIO_DIR")
    if raw is None or not str(raw).strip():
        raise ScenarioConfigurationError("SCENARIO_DIR is required and must name an absolute scenario package directory.")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ScenarioConfigurationError("SCENARIO_DIR must be an absolute path.")
    root = root.resolve()
    if not root.is_dir():
        raise ScenarioConfigurationError(f"Scenario directory is not readable: '{root}'.")
    return root


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioConfigurationError(f"Could not read valid scenario manifest at '{path}'.") from exc
    if not isinstance(payload, dict):
        raise ScenarioConfigurationError("Scenario manifest must be a JSON object.")
    return payload


def _database_fingerprint(root: Path, manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    csvs = manifest.get("csv_files", {})
    optional = manifest.get("optional_csv_files", {}) or {}
    for key, relative in sorted({**csvs, **optional}.items()):
        path = resolve_beneath(root, str(relative))
        if not path.exists() and key in optional:
            continue
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_scenario_package(explicit: Path | str | None = None) -> ScenarioPackage:
    root = scenario_root(explicit)
    manifest = _read_manifest(root)
    if manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise ScenarioConfigurationError(f"Unsupported scenario schema_version {manifest.get('schema_version')!r}.")
    for key in ("scenario_id", "scenario_version", "title", "calendar_start_date"):
        if not str(manifest.get(key) or "").strip():
            raise ScenarioConfigurationError(f"Scenario manifest requires {key!r}.")
    if "static_assets" in manifest:
        raise ScenarioConfigurationError("Scenario manifest field 'static_assets' is no longer supported.")
    try:
        epoch = date.fromisoformat(str(manifest["calendar_start_date"]))
    except ValueError as exc:
        raise ScenarioConfigurationError("calendar_start_date must be an ISO date.") from exc
    csvs = manifest.get("csv_files")
    if not isinstance(csvs, dict) or not REQUIRED_CSV_KEYS.issubset(csvs):
        missing = sorted(REQUIRED_CSV_KEYS - set(csvs or {}))
        raise ScenarioConfigurationError(f"Scenario manifest lacks required CSV mappings: {', '.join(missing)}.")
    unique_ids = {
        "terrain_types.csv": "terrain_id", "locations.csv": "location_id",
        "commanders.csv": "commander_id", "armies.csv": "army_id",
        "detachments.csv": "detachment_id", "strongholds.csv": "stronghold_id",
    }
    for relative in list(csvs.values()) + list((manifest.get("optional_csv_files") or {}).values()):
        path = resolve_beneath(root, str(relative))
        if not path.is_file():
            raise ScenarioConfigurationError(f"Scenario file is missing: '{path}'.")
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            if not reader.fieldnames:
                raise ValueError("CSV has no header")
            identity = unique_ids.get(Path(str(relative)).name)
            if rows and identity and identity in reader.fieldnames:
                values = [str(row.get(identity) or "").strip() for row in rows]
                if "" in values or len(values) != len(set(values)):
                    raise ValueError(f"missing or duplicate {identity}")
        except (OSError, csv.Error, ValueError) as exc:
            raise ScenarioConfigurationError(f"Malformed scenario CSV: '{path}'.") from exc
    for key in REQUIRED_PATH_KEYS:
        path = resolve_beneath(root, str(manifest.get(key) or ""))
        if key == "portraits_dir":
            if not path.is_dir():
                raise ScenarioConfigurationError(f"Scenario portrait directory is missing: '{path}'.")
        elif not path.is_file():
            raise ScenarioConfigurationError(f"Scenario file is missing for {key!r}: '{path}'.")
    points = resolve_beneath(root, str(manifest["stronghold_points"]))
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        if not points.with_suffix(suffix).is_file():
            raise ScenarioConfigurationError(f"Stronghold point component is missing: '{points.with_suffix(suffix)}'.")
    return ScenarioPackage(root, manifest, epoch, _database_fingerprint(root, manifest))


@lru_cache(maxsize=1)
def get_scenario_package() -> ScenarioPackage:
    return load_scenario_package()


def clear_scenario_cache() -> None:
    get_scenario_package.cache_clear()


def validate_database_binding(engine=None) -> None:
    from sqlalchemy import inspect
    from forwantofanail.core.database import create_session, get_engine
    from forwantofanail.core.models import ScenarioRuntime
    package = get_scenario_package()
    engine = engine or get_engine()
    if "scenario_runtime" not in inspect(engine).get_table_names():
        raise ScenarioConfigurationError("Database has no scenario binding; run Alembic and scenario bind-existing or reset.")
    session = create_session(engine)
    try:
        row = session.get(ScenarioRuntime, 1)
        if row is None:
            raise ScenarioConfigurationError("Database is not bound to a scenario; run scenario bind-existing or reset.")
        if row.scenario_id != package.scenario_id or row.database_source_fingerprint != package.database_source_fingerprint:
            raise ScenarioConfigurationError("Configured scenario does not match the database binding.")
    finally:
        session.close()


def bind_existing(engine=None) -> None:
    from forwantofanail.core.database import create_session, get_engine
    from forwantofanail.core.models import Commander, Location, ScenarioRuntime, Stronghold, TerrainType
    package = get_scenario_package()
    engine = engine or get_engine()
    session = create_session(engine)
    try:
        checks = (("terrain_types", TerrainType, "terrain_id"), ("locations", Location, "location_id"),
                  ("commanders", Commander, "commander_id"), ("strongholds", Stronghold, "stronghold_id"))
        for csv_key, model, id_field in checks:
            with package.csv_path(csv_key).open(encoding="utf-8-sig", newline="") as handle:
                expected = {str(row[id_field]).strip() for row in csv.DictReader(handle)}
            actual = {str(value[0]) for value in session.query(getattr(model, id_field)).all()}
            if not expected.issubset(actual):
                raise ScenarioConfigurationError(f"Database is missing immutable {csv_key} identities from the scenario package.")
        row = session.get(ScenarioRuntime, 1)
        if row is not None and row.scenario_id != package.scenario_id:
            raise ScenarioConfigurationError(
                f"Database is already bound to scenario {row.scenario_id!r}, not {package.scenario_id!r}."
            )
        row = row or ScenarioRuntime(singleton_id=1)
        row.scenario_id = package.scenario_id
        row.scenario_version = package.scenario_version
        row.database_source_fingerprint = package.database_source_fingerprint
        row.bound_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and bind external For Want of a Nail scenarios.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "bind-existing"):
        item = sub.add_parser(name)
        item.add_argument("--scenario-dir", type=Path)
    args = parser.parse_args()
    if args.scenario_dir:
        os.environ["SCENARIO_DIR"] = str(args.scenario_dir.resolve())
        clear_scenario_cache()
    package = get_scenario_package()
    if args.command == "bind-existing":
        bind_existing()
        print(f"Bound database to {package.scenario_id} version {package.scenario_version}.")
    else:
        from forwantofanail.core.scenario_catalog import build_historical_stronghold_catalog_for_scenario
        build_historical_stronghold_catalog_for_scenario(package.root)
        print(f"Valid scenario: {package.title} ({package.scenario_id}).")


if __name__ == "__main__":
    main()
