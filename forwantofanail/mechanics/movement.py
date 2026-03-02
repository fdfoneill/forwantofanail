from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwantofanail.core.models import Army, Detachment, Location, Movement, TerrainType
from forwantofanail.mechanics.time import GameTime, Watch, advance_time


RIVER_TERRAIN_NAME = "River"


def _get_h3():
    try:
        import h3
    except ImportError as exc:
        raise RuntimeError("Uber h3-py library is required for adjacency checks. Install `h3`.") from exc
    return h3


def _neighbors(location_id: str) -> set[str]:
    h3 = _get_h3()
    return set(h3.grid_ring(location_id, 1))


def are_adjacent(origin_id: str, destination_id: str) -> bool:
    return destination_id in _neighbors(origin_id)


def _has_wagons(army: Army) -> bool:
    return any(detachment.wagon_count > 0 for detachment in army.detachments)


def _has_infantry(army: Army) -> bool:
    if not army.detachments:
        return True
    return any(not detachment.is_cavalry for detachment in army.detachments)


def _terrain(session: Session, location: Location) -> TerrainType:
    terrain = session.get(TerrainType, location.terrain_id)
    if terrain is None:
        raise ValueError(f"Unknown terrain_id {location.terrain_id} for location {location.location_id}")
    return terrain


def _is_river(terrain: TerrainType) -> bool:
    return terrain.terrain_name.strip().lower() == RIVER_TERRAIN_NAME.lower()


def _movement_cost(session: Session, army: Army, origin: Location, destination: Location) -> int:
    on_road = origin.is_road and destination.is_road
    has_wagons = _has_wagons(army)
    if not on_road and has_wagons:
        raise ValueError("Armies with wagons cannot move off-road.")

    terrain = _terrain(session, destination)
    if terrain.is_water and not _is_river(terrain) and not army.is_embarked:
        raise ValueError("Armies must be embarked to enter open water.")

    if _is_river(terrain) and not on_road:
        if has_wagons:
            raise ValueError("Armies with wagons cannot enter river cells off-road.")
        if _has_infantry(army):
            return 5

    base_cost = 1 if on_road else 2
    if on_road:
        return base_cost

    multiplier = terrain.speed_multiplier or 1.0
    if multiplier <= 0:
        multiplier = 1.0
    return int(math.ceil(base_cost / multiplier))


def _crosses_night(start_watch: Watch, steps: int) -> bool:
    watch = start_watch
    for _ in range(steps):
        next_index = (int(watch) + 1) % 5
        watch = Watch(next_index)
        if watch == Watch.NIGHT:
            return True
    return False


@dataclass(frozen=True)
class MoveResult:
    army_id: int
    origin_id: str
    destination_id: str
    watches_spent: int
    game_time: GameTime


def move_army(
    session: Session,
    army_id: int,
    destination_id: str,
    game_time: GameTime,
    allow_night: bool = False,
) -> MoveResult:
    army = session.get(Army, army_id)
    if army is None:
        raise ValueError(f"Unknown army_id {army_id}")

    destination = session.get(Location, destination_id)
    if destination is None:
        raise ValueError(f"Unknown destination {destination_id}")

    origin = army.location
    if origin is None:
        raise ValueError(f"Army {army_id} has no current location.")

    if not are_adjacent(origin.location_id, destination.location_id):
        raise ValueError("Destination is not adjacent to current location.")

    watches_spent = _movement_cost(session, army, origin, destination)
    if not allow_night:
        if game_time.watch == Watch.NIGHT:
            raise ValueError("Cannot move during Night watch without allow_night.")
        if _crosses_night(game_time.watch, watches_spent):
            raise ValueError("Movement would cross Night watch without allow_night.")

    next_date, next_watch = advance_time(game_time.date, game_time.watch, watches_spent)
    army.location = destination
    movement = Movement(
        army_id=army.army_id,
        location_id=destination.location_id,
        date=next_date,
        watch=int(next_watch),
    )
    session.add(movement)
    session.flush()

    return MoveResult(
        army_id=army.army_id,
        origin_id=origin.location_id,
        destination_id=destination.location_id,
        watches_spent=watches_spent,
        game_time=GameTime(date=next_date, watch=next_watch),
    )


def calculate_move_watches(session: Session, army_id: int, destination_id: str) -> int:
    army = session.get(Army, army_id)
    if army is None:
        raise ValueError(f"Unknown army_id {army_id}")

    destination = session.get(Location, destination_id)
    if destination is None:
        raise ValueError(f"Unknown destination {destination_id}")

    origin = army.location
    if origin is None:
        raise ValueError(f"Army {army_id} has no current location.")

    if not are_adjacent(origin.location_id, destination.location_id):
        raise ValueError("Destination is not adjacent to current location.")

    return _movement_cost(session, army, origin, destination)


def list_valid_destinations(session: Session, army_id: int) -> list[str]:
    army = session.get(Army, army_id)
    if army is None:
        raise ValueError(f"Unknown army_id {army_id}")

    origin = army.location
    if origin is None:
        raise ValueError(f"Army {army_id} has no current location.")

    neighbor_ids = _neighbors(origin.location_id)
    candidates = (
        session.query(Location)
        .filter(Location.location_id.in_(neighbor_ids))
        .all()
    )

    valid = []
    for destination in candidates:
        try:
            _movement_cost(session, army, origin, destination)
        except ValueError:
            continue
        valid.append(destination.location_id)
    return valid
