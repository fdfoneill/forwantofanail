from __future__ import annotations

import argparse
import csv
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
    Stronghold,
    TerrainType,
)


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


def _load_csv(model_cls, csv_path: Path, converters: dict[str, callable]):
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            payload = {}
            for key, value in row.items():
                converter = converters.get(key)
                payload[key] = converter(value) if converter else value
            yield model_cls(**payload)


def initialize_database(data_dir: Path, reset: bool = False) -> None:
    engine = get_engine()
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    session = create_session(engine)
    try:
        session.add_all(
            _load_csv(
                TerrainType,
                data_dir / "terrain_types.csv",
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
                data_dir / "locations.csv",
                {
                    "location_id": str,
                    "is_road": _parse_bool,
                    "region": str,
                    "terrain_id": _parse_int,
                    "settlement": _parse_int,
                    "foraged_this_season": _parse_bool,
                },
            )
        )
        session.add_all(
            _load_csv(
                Commander,
                data_dir / "commanders.csv",
                {
                    "commander_id": _parse_int,
                    "commander_name": str,
                    "commander_age": _parse_int,
                    "commander_title": str,
                },
            )
        )
        session.add_all(
            _load_csv(
                Army,
                data_dir / "armies.csv",
                {
                    "army_id": _parse_int,
                    "location_id": str,
                    "army_name": str,
                    "army_faction": str,
                    "commander_id": _parse_int,
                    "army_supply": _parse_int,
                    "army_morale": _parse_int,
                    "is_embarked": _parse_bool,
                    "is_garrison": _parse_bool,
                    "noncombattant_count": _parse_int,
                },
            )
        )
        session.add_all(
            _load_csv(
                Detachment,
                data_dir / "detachments.csv",
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
        session.add_all(
            _load_csv(
                DetachmentSpecial,
                data_dir / "detachment_specials.csv",
                {
                    "detachment_id": _parse_int,
                    "special_name": str,
                },
            )
        )
        session.add_all(
            _load_csv(
                CommanderTrait,
                data_dir / "commander_traits.csv",
                {
                    "commander_id": _parse_int,
                    "trait_name": str,
                },
            )
        )
        session.add_all(
            _load_csv(
                Stronghold,
                data_dir / "strongholds.csv",
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
        movements_path = data_dir / "movements.csv"
        if movements_path.exists():
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
            session.add(GameClock(singleton_id=1, day=1, watch=1))

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
