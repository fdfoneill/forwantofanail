from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import date
from pathlib import Path

from forwantofanail.core.database import Base, create_session, get_engine
from forwantofanail.core.models import (
    Army,
    Commander,
    CommanderTrait,
    Detachment,
    DetachmentSpecial,
    GameClock,
    Location,
    Movement,
    Siege,
    Stronghold,
    TerrainType,
)


def _drop_all_tables_for_reset(engine) -> None:
    if engine.dialect.name != "sqlite":
        Base.metadata.drop_all(engine)
        return

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            Base.metadata.drop_all(connection)
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    text = value.strip().upper()
    if text == "":
        return None
    if text in {"TRUE", "1", "YES", "Y"}:
        return True
    if text in {"FALSE", "0", "NO", "N"}:
        return False
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    return int(text)


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    return float(text)


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    return date.fromisoformat(text)


def _clamp_morale(value: int | None, default: int = 9) -> int:
    if value is None:
        return default
    return max(2, min(12, int(value)))


def _clamp_noncombatant_percent(value: float | None, default: float = 0.25) -> float:
    if value is None:
        return default
    return max(0.0, float(value))


def _generated_garrison_composition(stronghold_type: str) -> list[dict[str, object]]:
    normalized = str(stronghold_type or "").strip().lower()
    if normalized == "town":
        return [{"suffix": "Infantry", "warriors": 250, "is_cavalry": False}]
    if normalized == "city":
        return [{"suffix": "Infantry", "warriors": 500, "is_cavalry": False}]
    if normalized == "fortress":
        return [
            {"suffix": "Infantry", "warriors": 250, "is_cavalry": False},
            {"suffix": "Cavalry", "warriors": 50, "is_cavalry": True},
        ]
    return [{"suffix": "Infantry", "warriors": 250, "is_cavalry": False}]


def _load_csv(model_cls, csv_path: Path, converters: dict[str, callable]):
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            payload = {}
            for key, value in row.items():
                converter = converters.get(key)
                payload[key] = converter(value) if converter else value
            yield model_cls(**payload)


def _default_scenario_manifest() -> dict[str, object]:
    return {
        "csv_files": {
            "terrain_types": "terrain_types.csv",
            "locations": "locations.csv",
            "commanders": "commanders.csv",
            "armies": "armies.csv",
            "detachments": "detachments.csv",
            "detachment_specials": "detachment_specials.csv",
            "commander_traits": "commander_traits.csv",
            "strongholds": "strongholds.csv",
        },
        "optional_csv_files": {
            "movements": "movements.csv",
        },
        "static_assets": [
            {
                "source": "assets/map_diegetic.png",
                "target": "map_diegetic.png",
                "allow_existing_target": True,
            }
        ],
    }


def _load_scenario_manifest(data_dir: Path) -> dict[str, object]:
    manifest_path = data_dir / "scenario_manifest.json"
    if not manifest_path.exists():
        return _default_scenario_manifest()
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Scenario manifest must be a JSON object: '{manifest_path}'.")
    manifest = _default_scenario_manifest()
    manifest.update(payload)
    return manifest


def _resolve_scenario_file(data_dir: Path, relative_path: str) -> Path:
    candidate = (data_dir / str(relative_path)).resolve()
    data_root = data_dir.resolve()
    if data_root not in (candidate, *candidate.parents):
        raise ValueError(f"Scenario path escapes data directory: '{relative_path}'.")
    return candidate


def _resolve_static_target(relative_path: str) -> Path:
    static_root = Path(__file__).resolve().parents[1] / "web" / "static"
    candidate = (static_root / str(relative_path)).resolve()
    if static_root.resolve() not in (candidate, *candidate.parents):
        raise ValueError(f"Static asset target escapes static directory: '{relative_path}'.")
    return candidate


def _manifest_csv_path(manifest: dict[str, object], data_dir: Path, key: str, *, optional: bool = False) -> Path | None:
    section_name = "optional_csv_files" if optional else "csv_files"
    section = manifest.get(section_name)
    if not isinstance(section, dict):
        if optional:
            return None
        raise ValueError(f"Scenario manifest section '{section_name}' must be an object.")
    relative_path = section.get(key)
    if relative_path is None:
        if optional:
            return None
        raise KeyError(f"Scenario manifest missing required csv mapping '{key}'.")
    return _resolve_scenario_file(data_dir, str(relative_path))


def _validate_scenario_manifest(manifest: dict[str, object], data_dir: Path) -> None:
    required_section = manifest.get("csv_files")
    if not isinstance(required_section, dict):
        raise ValueError("Scenario manifest section 'csv_files' must be an object.")
    for key in required_section:
        csv_path = _manifest_csv_path(manifest, data_dir, str(key), optional=False)
        if csv_path is None or not csv_path.exists():
            raise FileNotFoundError(f"Scenario reset requires CSV '{key}' at '{csv_path}'.")
    optional_section = manifest.get("optional_csv_files", {})
    if optional_section is not None and not isinstance(optional_section, dict):
        raise ValueError("Scenario manifest section 'optional_csv_files' must be an object.")
    static_assets = manifest.get("static_assets", [])
    if static_assets is None:
        static_assets = []
    if not isinstance(static_assets, list):
        raise ValueError("Scenario manifest section 'static_assets' must be a list.")
    for row in static_assets:
        if not isinstance(row, dict):
            raise ValueError("Each scenario static asset entry must be an object.")
        source = row.get("source")
        target = row.get("target")
        if not source or not target:
            raise ValueError("Each scenario static asset entry requires 'source' and 'target'.")
        _resolve_scenario_file(data_dir, str(source))
        _resolve_static_target(str(target))


def _prepare_scenario_static_assets(manifest: dict[str, object], data_dir: Path) -> None:
    static_assets = manifest.get("static_assets", [])
    for row in static_assets if isinstance(static_assets, list) else []:
        source_path = _resolve_scenario_file(data_dir, str(row["source"]))
        target_path = _resolve_static_target(str(row["target"]))
        allow_existing_target = bool(row.get("allow_existing_target"))
        if source_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
            continue
        if allow_existing_target and target_path.exists():
            continue
        raise FileNotFoundError(
            f"Scenario reset requires asset at either '{source_path}' or existing target '{target_path}'."
        )


def initialize_database(data_dir: Path, reset: bool = False) -> None:
    manifest = _load_scenario_manifest(data_dir)
    _validate_scenario_manifest(manifest, data_dir)
    engine = get_engine()
    if reset:
        _prepare_scenario_static_assets(manifest, data_dir)
        _drop_all_tables_for_reset(engine)
    Base.metadata.create_all(engine)

    session = create_session(engine)
    try:
        session.add_all(
            _load_csv(
                TerrainType,
                _manifest_csv_path(manifest, data_dir, "terrain_types"),
                {
                    "terrain_id": _parse_int,
                    "terrain_name": str,
                    "speed_multiplier": _parse_float,
                    "scout_multiplier": _parse_float,
                    "is_water": _parse_bool,
                },
            )
        )
        session.add_all(
            _load_csv(
                Location,
                _manifest_csv_path(manifest, data_dir, "locations"),
                {
                    "location_id": str,
                    "is_road": _parse_bool,
                    "region": str,
                    "terrain_id": _parse_int,
                    "settlement": _parse_int,
                    "foraged_this_season": _parse_int,
                },
            )
        )
        session.add_all(
            _load_csv(
                Commander,
                _manifest_csv_path(manifest, data_dir, "commanders"),
                {
                    "commander_id": _parse_int,
                    "commander_name": str,
                    "commander_age": _parse_int,
                    "commander_title": str,
                },
            )
        )
        armies: list[Army] = []
        source_noncombatants_by_army_id: dict[int, int] = {}
        armies_path = _manifest_csv_path(manifest, data_dir, "armies")
        with armies_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                army_id = _parse_int(row.get("army_id"))
                if army_id is None:
                    continue
                noncombatants = _parse_int(row.get("noncombattant_count"))
                source_noncombatants_by_army_id[army_id] = int(noncombatants or 0)
                armies.append(
                    Army(
                        army_id=army_id,
                        location_id=(row.get("location_id") or "").strip(),
                        army_name=(row.get("army_name") or "").strip(),
                        army_faction=(row.get("army_faction") or "").strip(),
                        commander_id=_parse_int(row.get("commander_id")),
                        army_supply=int(_parse_int(row.get("army_supply")) or 0),
                        army_morale=_parse_int(row.get("army_morale")),
                        is_embarked=bool(_parse_bool(row.get("is_embarked"))),
                        is_garrison=bool(_parse_bool(row.get("is_garrison"))),
                        noncombattant_percent=0.25,
                    )
                )
        for army in armies:
            army.army_morale = _clamp_morale(army.army_morale)
            # Resting morale starts equal to the initial current morale.
            army.army_resting_morale = army.army_morale
        session.add_all(armies)
        detachments = list(
            _load_csv(
                Detachment,
                _manifest_csv_path(manifest, data_dir, "detachments"),
                {
                    "detachment_id": _parse_int,
                    "detachment_name": str,
                    "army_id": _parse_int,
                    "is_heavy": _parse_bool,
                    "is_cavalry": _parse_bool,
                    "wagon_count": _parse_int,
                    "warrior_count": _parse_int,
                    "is_mercenary": _parse_bool,
                },
            )
        )
        session.add_all(detachments)
        session.flush()
        fighting_strength_by_army_id: dict[int, int] = {}
        for detachment in detachments:
            fighting_strength_by_army_id.setdefault(detachment.army_id, 0)
            fighting_strength_by_army_id[detachment.army_id] += int(detachment.warrior_count or 0)
        for army in armies:
            source_noncombatants = int(source_noncombatants_by_army_id.get(army.army_id, 0))
            fighting_strength = int(fighting_strength_by_army_id.get(army.army_id, 0))
            if fighting_strength <= 0:
                army.noncombattant_percent = 0.25
            else:
                army.noncombattant_percent = _clamp_noncombatant_percent(
                    source_noncombatants / fighting_strength,
                    default=0.25,
                )
        session.add_all(
            _load_csv(
                DetachmentSpecial,
                _manifest_csv_path(manifest, data_dir, "detachment_specials"),
                {
                    "detachment_id": _parse_int,
                    "special_name": str,
                },
            )
        )
        session.add_all(
            _load_csv(
                CommanderTrait,
                _manifest_csv_path(manifest, data_dir, "commander_traits"),
                {
                    "commander_id": _parse_int,
                    "trait_name": str,
                },
            )
        )
        session.add_all(
            _load_csv(
                Stronghold,
                _manifest_csv_path(manifest, data_dir, "strongholds"),
                {
                    "stronghold_id": _parse_int,
                    "location_id": str,
                    "stronghold_name": str,
                    "stronghold_type": str,
                    "control": str,
                    "stronghold_threshold": _parse_int,
                },
            )
        )
        session.flush()

        strongholds = session.query(Stronghold).order_by(Stronghold.stronghold_id.asc()).all()
        next_army_id = max((int(army.army_id or 0) for army in armies), default=0) + 1
        next_detachment_id = max((int(det.detachment_id or 0) for det in detachments), default=0) + 1
        generated_garrison_armies: list[Army] = []
        generated_garrison_detachments: list[Detachment] = []
        for stronghold in strongholds:
            garrison_army = Army(
                army_id=next_army_id,
                location_id=stronghold.location_id,
                army_name=f"{stronghold.stronghold_name} Garrison",
                army_faction=stronghold.control,
                commander_id=None,
                garrison_stronghold_id=stronghold.stronghold_id,
                army_supply=0,
                army_morale=9,
                army_resting_morale=9,
                is_embarked=False,
                is_garrison=True,
                noncombattant_percent=0.0,
            )
            generated_garrison_armies.append(garrison_army)
            next_army_id += 1
            for row in _generated_garrison_composition(stronghold.stronghold_type):
                generated_garrison_detachments.append(
                    Detachment(
                        detachment_id=next_detachment_id,
                        detachment_name=f"{stronghold.stronghold_name} Garrison {row['suffix']}",
                        army_id=garrison_army.army_id,
                        is_heavy=False,
                        is_cavalry=bool(row["is_cavalry"]),
                        wagon_count=0,
                        warrior_count=int(row["warriors"]),
                        is_mercenary=False,
                    )
                )
                next_detachment_id += 1
        if generated_garrison_armies:
            session.add_all(generated_garrison_armies)
            armies.extend(generated_garrison_armies)
        if generated_garrison_detachments:
            session.add_all(generated_garrison_detachments)
            detachments.extend(generated_garrison_detachments)
        movements_path = _manifest_csv_path(manifest, data_dir, "movements", optional=True)
        if movements_path is not None and movements_path.exists():
            session.add_all(
                _load_csv(
                    Movement,
                    movements_path,
                    {
                        "army_id": _parse_int,
                        "location_id": str,
                        "date": _parse_date,
                        "watch": _parse_int,
                    },
                )
            )

        if session.get(GameClock, 1) is None:
            session.add(GameClock(singleton_id=1, day=1, watch=1, world_tick=0))

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the game database from CSV data.")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate tables before loading.")
    args = parser.parse_args()

    initialize_database(args.data_dir, reset=args.reset)


if __name__ == "__main__":
    main()
