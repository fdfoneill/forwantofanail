from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwantofanail.core.models import Army, Detachment, Location, Movement, Stronghold, TerrainType
from forwantofanail.mechanics.time import GameTime, Watch, advance_time


RIVER_TERRAIN_NAME = "River"
OPEN_WATER_TERRAIN_NAME = "Open Water"


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


def _is_stronghold_location(session: Session, location_id: str) -> bool:
    row = (
        session.query(Stronghold.stronghold_id)
        .filter(Stronghold.location_id == location_id)
        .first()
    )
    return row is not None


def _stronghold_has_hostile_occupants(session: Session, destination: Location, army: Army) -> bool:
    if not _is_stronghold_location(session, destination.location_id):
        return False
    blocker = (
        session.query(Army.army_id)
        .join(Detachment, Detachment.army_id == Army.army_id)
        .filter(
            Army.location_id == destination.location_id,
            Army.army_id != army.army_id,
            Army.army_faction != army.army_faction,
            Detachment.warrior_count > 0,
        )
        .first()
    )
    return blocker is not None


def _is_river(terrain: TerrainType) -> bool:
    return terrain.terrain_name.strip().lower() == RIVER_TERRAIN_NAME.lower()


def _is_open_water(terrain: TerrainType) -> bool:
    return terrain.terrain_name.strip().lower() == OPEN_WATER_TERRAIN_NAME.lower()


def _movement_cost(session: Session, army: Army, origin: Location, destination: Location) -> int:
    origin_is_stronghold = _is_stronghold_location(session, origin.location_id)
    destination_is_stronghold = _is_stronghold_location(session, destination.location_id)
    on_road = origin.is_road and destination.is_road

    # Movement into a stronghold is always permitted, even if the stronghold cell itself
    # is not marked as road. Moving out of a stronghold only gets "road-like" treatment
    # when stepping onto an actual road cell.
    moving_into_stronghold = destination_is_stronghold
    moving_out_stronghold_to_road = origin_is_stronghold and destination.is_road
    effective_on_road = on_road or moving_into_stronghold or moving_out_stronghold_to_road

    has_wagons = _has_wagons(army)
    if _stronghold_has_hostile_occupants(session, destination, army):
        raise ValueError("Hostile occupied strongholds must be besieged or assaulted.")
    if not effective_on_road and has_wagons:
        raise ValueError("Armies with wagons cannot move off-road.")

    terrain = _terrain(session, destination)
    if terrain.is_water and not _is_river(terrain) and not effective_on_road and not army.is_embarked and not destination_is_stronghold:
        raise ValueError("Armies must be embarked to enter open water.")

    if _is_river(terrain) and not effective_on_road and not destination_is_stronghold:
        if has_wagons:
            raise ValueError("Armies with wagons cannot enter river cells off-road.")
        if _has_infantry(army):
            return 5

    base_cost = 1 if effective_on_road else 2
    if effective_on_road:
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


@dataclass(frozen=True)
class StrongholdRouteResult:
    path: list[str]
    total_cost: int
    offroad_allowed: bool
    used_offroad: bool


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


def list_valid_destinations_from_origin(session: Session, army_id: int, origin_id: str) -> list[str]:
    army = session.get(Army, army_id)
    if army is None:
        raise ValueError(f"Unknown army_id {army_id}")

    origin = session.get(Location, origin_id)
    if origin is None:
        raise ValueError(f"Unknown origin {origin_id}")

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


def calculate_move_watches_from_origin(
    session: Session,
    army_id: int,
    origin_id: str,
    destination_id: str,
) -> int:
    army = session.get(Army, army_id)
    if army is None:
        raise ValueError(f"Unknown army_id {army_id}")
    origin = session.get(Location, origin_id)
    if origin is None:
        raise ValueError(f"Unknown origin {origin_id}")
    destination = session.get(Location, destination_id)
    if destination is None:
        raise ValueError(f"Unknown destination {destination_id}")
    if not are_adjacent(origin.location_id, destination.location_id):
        raise ValueError("Destination is not adjacent to origin.")
    return _movement_cost(session, army, origin, destination)


def _route_step_cost(
    session: Session,
    army: Army,
    origin: Location,
    destination: Location,
    *,
    on_road_only: bool,
    destination_is_stronghold: bool,
) -> int | None:
    origin_is_stronghold = _is_stronghold_location(session, origin.location_id)
    on_road = origin.is_road and destination.is_road
    moving_into_stronghold = destination_is_stronghold
    moving_out_stronghold_to_road = origin_is_stronghold and destination.is_road
    effective_on_road = on_road or moving_into_stronghold or moving_out_stronghold_to_road

    if on_road_only and not effective_on_road:
        return None

    terrain = _terrain(session, destination)
    if _is_open_water(terrain) and not destination_is_stronghold:
        return None

    if effective_on_road:
        return 1

    if _is_river(terrain) and not destination_is_stronghold:
        return 4
    return 2


def find_stronghold_route(
    session: Session,
    *,
    army: Army,
    start_id: str,
    destination_id: str,
    avoid_location_ids: set[str] | None = None,
    on_road_only: bool = False,
) -> StrongholdRouteResult:
    start = session.get(Location, start_id)
    destination = session.get(Location, destination_id)
    if start is None:
        raise ValueError(f"Unknown start location {start_id}")
    if destination is None:
        raise ValueError(f"Unknown destination location {destination_id}")

    offroad_allowed = not _has_wagons(army)
    if not offroad_allowed:
        on_road_only = True

    avoid_set = set(avoid_location_ids or set())
    avoid_set.discard(start_id)
    avoid_set.discard(destination_id)

    locations = {
        row.location_id: row
        for row in session.query(Location).all()
    }
    stronghold_location_ids = {
        row[0]
        for row in session.query(Stronghold.location_id).all()
    }

    frontier: list[tuple[int, int, str]] = [(0, 0, start_id)]
    costs: dict[str, int] = {start_id: 0}
    previous: dict[str, str] = {}
    steps: dict[str, int] = {start_id: 0}

    while frontier:
        total_cost, step_count, current_id = heapq.heappop(frontier)
        if total_cost != costs.get(current_id):
            continue
        if current_id == destination_id:
            break
        for neighbor_id in sorted(_neighbors(current_id)):
            if neighbor_id in avoid_set:
                continue
            neighbor = locations.get(neighbor_id)
            current = locations.get(current_id)
            if neighbor is None or current is None:
                continue
            is_stronghold = neighbor_id in stronghold_location_ids
            step_cost = _route_step_cost(
                session,
                army,
                current,
                neighbor,
                on_road_only=on_road_only,
                destination_is_stronghold=is_stronghold,
            )
            if step_cost is None:
                continue
            next_cost = total_cost + step_cost
            next_steps = step_count + 1
            known_cost = costs.get(neighbor_id)
            known_steps = steps.get(neighbor_id, 10**9)
            if known_cost is None or next_cost < known_cost or (next_cost == known_cost and next_steps < known_steps):
                costs[neighbor_id] = next_cost
                steps[neighbor_id] = next_steps
                previous[neighbor_id] = current_id
                heapq.heappush(frontier, (next_cost, next_steps, neighbor_id))

    if destination_id not in costs:
        raise ValueError("No valid route found under the requested constraints.")

    path = [destination_id]
    current_id = destination_id
    while current_id != start_id:
        current_id = previous[current_id]
        path.append(current_id)
    path.reverse()

    used_offroad = False
    for index in range(1, len(path)):
        origin = locations[path[index - 1]]
        next_location = locations[path[index]]
        origin_is_stronghold = path[index - 1] in stronghold_location_ids
        destination_is_stronghold = path[index] in stronghold_location_ids
        on_road = origin.is_road and next_location.is_road
        moving_into_stronghold = destination_is_stronghold
        moving_out_stronghold_to_road = origin_is_stronghold and next_location.is_road
        effective_on_road = on_road or moving_into_stronghold or moving_out_stronghold_to_road
        if not effective_on_road:
            used_offroad = True
            break

    return StrongholdRouteResult(
        path=path,
        total_cost=costs[destination_id],
        offroad_allowed=offroad_allowed,
        used_offroad=used_offroad,
    )
