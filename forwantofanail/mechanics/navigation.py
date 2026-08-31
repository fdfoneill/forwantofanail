from __future__ import annotations

from dataclasses import dataclass
import heapq
import math

import h3
from sqlalchemy.orm import Session, joinedload

from forwantofanail.core.models import Army, Location, Stronghold


class RouteNotFoundError(ValueError):
    """Raised when the requested strategic route does not exist."""


@dataclass(frozen=True)
class _MobilityProfile:
    has_wagons: bool
    has_infantry: bool
    is_embarked: bool


@dataclass(frozen=True)
class _Edge:
    watches: int
    travel: str
    terrain: str


_DIRECTION_ADJECTIVES = {
    "north": "northern",
    "northeast": "northeastern",
    "east": "eastern",
    "southeast": "southeastern",
    "south": "southern",
    "southwest": "southwestern",
    "west": "western",
    "northwest": "northwestern",
}


def _neighbors(cell_h3: str, known_cells: set[str]) -> list[str]:
    try:
        return sorted(str(cell) for cell in h3.grid_ring(cell_h3, 1) if str(cell) in known_cells)
    except Exception:
        return []


def _bearing_word(origin_h3: str, destination_h3: str) -> str | None:
    try:
        origin_lat, origin_lng = h3.cell_to_latlng(origin_h3)
        destination_lat, destination_lng = h3.cell_to_latlng(destination_h3)
    except Exception:
        return None
    lat_1 = math.radians(origin_lat)
    lat_2 = math.radians(destination_lat)
    delta_lng = math.radians(destination_lng - origin_lng)
    x = math.sin(delta_lng) * math.cos(lat_2)
    y = (
        math.cos(lat_1) * math.sin(lat_2)
        - math.sin(lat_1) * math.cos(lat_2) * math.cos(delta_lng)
    )
    bearing = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    directions = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
    return directions[int((bearing + 22.5) // 45.0) % 8]


def _mobility_profile(army: Army) -> _MobilityProfile:
    detachments = list(army.detachments or [])
    return _MobilityProfile(
        has_wagons=any(int(detachment.wagon_count or 0) > 0 for detachment in detachments),
        has_infantry=not detachments or any(not bool(detachment.is_cavalry) for detachment in detachments),
        is_embarked=bool(army.is_embarked),
    )


def _static_edge(
    origin: Location,
    destination: Location,
    *,
    stronghold_cells: set[str],
    mobility: _MobilityProfile,
) -> _Edge | None:
    origin_is_stronghold = origin.location_id in stronghold_cells
    destination_is_stronghold = destination.location_id in stronghold_cells
    effective_on_road = bool(
        (origin.is_road and destination.is_road)
        or destination_is_stronghold
        or (origin_is_stronghold and destination.is_road)
    )
    road_travel = bool(
        (origin.is_road and destination.is_road)
        or (origin.is_road and destination_is_stronghold)
        or (origin_is_stronghold and destination.is_road)
    )
    terrain = destination.terrain_type
    if terrain is None:
        return None
    terrain_name = str(terrain.terrain_name or "unknown terrain").strip()
    normalized_terrain = terrain_name.casefold()

    if not effective_on_road and mobility.has_wagons:
        return None
    if (
        bool(terrain.is_water)
        and normalized_terrain != "river"
        and not effective_on_road
        and not mobility.is_embarked
        and not destination_is_stronghold
    ):
        return None
    if normalized_terrain == "river" and not effective_on_road and not destination_is_stronghold:
        if mobility.has_wagons:
            return None
        if mobility.has_infantry:
            return _Edge(watches=5, travel="off_road", terrain=terrain_name)

    if effective_on_road:
        return _Edge(
            watches=1,
            travel="road" if road_travel else "off_road",
            terrain=terrain_name,
        )
    multiplier = float(terrain.speed_multiplier or 1.0)
    if multiplier <= 0:
        multiplier = 1.0
    return _Edge(watches=int(math.ceil(2 / multiplier)), travel="off_road", terrain=terrain_name)


def _shortest_path(
    *,
    start_h3: str,
    destination_h3: str,
    locations: dict[str, Location],
    stronghold_cells: set[str],
    mobility: _MobilityProfile,
    allow_off_road: bool,
) -> tuple[list[str], list[_Edge]]:
    if start_h3 == destination_h3:
        return [start_h3], []

    known_cells = set(locations)
    frontier: list[tuple[int, int, tuple[str, ...], str, tuple[_Edge, ...]]] = [
        (0, 0, (start_h3,), start_h3, ())
    ]
    best: dict[str, tuple[int, int, tuple[str, ...]]] = {start_h3: (0, 0, (start_h3,))}

    while frontier:
        cost, steps, path, cell_h3, edges = heapq.heappop(frontier)
        if best.get(cell_h3) != (cost, steps, path):
            continue
        if cell_h3 == destination_h3:
            return list(path), list(edges)
        origin = locations[cell_h3]
        for neighbor_h3 in _neighbors(cell_h3, known_cells):
            destination = locations[neighbor_h3]
            edge = _static_edge(
                origin,
                destination,
                stronghold_cells=stronghold_cells,
                mobility=mobility,
            )
            if edge is None or (not allow_off_road and edge.travel != "road"):
                continue
            next_path = (*path, neighbor_h3)
            candidate = (cost + edge.watches, steps + 1, next_path)
            if neighbor_h3 in best and best[neighbor_h3] <= candidate:
                continue
            best[neighbor_h3] = candidate
            heapq.heappush(
                frontier,
                (candidate[0], candidate[1], next_path, neighbor_h3, (*edges, edge)),
            )
    raise RouteNotFoundError("No route matching those travel restrictions was found.")


def _path_landmarks(
    path: list[str],
    *,
    origin_name: str,
    origin_ref: str | None,
    destination: Stronghold,
    strongholds: list[Stronghold],
    locations: dict[str, Location],
) -> list[tuple[int, str, str | None]]:
    """Return path-index, display-name, and optional stronghold ref."""
    landmarks: list[tuple[int, str, str | None]] = [(0, origin_name, origin_ref)]
    destination_id = int(destination.stronghold_id)
    candidates: list[tuple[int, int, Stronghold]] = []
    for stronghold in strongholds:
        if int(stronghold.stronghold_id) == destination_id or stronghold.location_id == path[0]:
            continue
        exact_index = next(
            (index for index in range(1, len(path) - 1) if stronghold.location_id == path[index]),
            None,
        )
        if exact_index is not None:
            candidates.append((exact_index, 0, stronghold))
            continue
        for index in range(1, len(path) - 1):
            path_cell = path[index]
            if bool(locations[path_cell].is_road) and stronghold.location_id in _neighbors(path_cell, set(locations)):
                candidates.append((index, 1, stronghold))
                break

    used_indexes: set[int] = set()
    for index, _adjacent_rank, stronghold in sorted(
        candidates,
        key=lambda item: (item[0], item[1], int(item[2].stronghold_id)),
    ):
        if index in used_indexes:
            continue
        used_indexes.add(index)
        landmarks.append(
            (index, str(stronghold.stronghold_name), f"sh_{int(stronghold.stronghold_id)}")
        )
    landmarks.append(
        (len(path) - 1, str(destination.stronghold_name), f"sh_{destination_id}")
    )
    return sorted(landmarks, key=lambda item: item[0])


def _join_phrases(parts: list[str]) -> str:
    if len(parts) < 2:
        return parts[0] if parts else ""
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _leg_phrase(leg: dict[str, object]) -> str:
    leagues = int(leg["leagues"])
    unit = "league" if leagues == 1 else "leagues"
    destination = str(leg["to"])
    travel = str(leg["travel"])
    if travel == "road":
        return f"{leagues} {unit} by road to {destination}"
    terrains = [str(value).lower() for value in leg.get("terrains", [])]
    terrain_phrase = f" through {_join_phrases(terrains)}" if terrains else ""
    if travel == "off_road":
        return f"{leagues} {unit} off-road{terrain_phrase} to {destination}"
    return f"{leagues} {unit} by road and off-road{terrain_phrase} to {destination}"


def build_route_summary(
    session: Session,
    *,
    army: Army,
    origin: Stronghold | None,
    destination: Stronghold,
    allow_off_road: bool,
) -> dict[str, object]:
    """Build a strategic, static-world route without exposing its H3 path."""
    locations_list = (
        session.query(Location)
        .options(joinedload(Location.terrain_type))
        .order_by(Location.location_id.asc())
        .all()
    )
    locations = {str(location.location_id): location for location in locations_list}
    strongholds = session.query(Stronghold).order_by(Stronghold.stronghold_id.asc()).all()
    stronghold_cells = {str(stronghold.location_id) for stronghold in strongholds}
    start_h3 = str(origin.location_id if origin is not None else army.location_id)
    destination_h3 = str(destination.location_id)
    if start_h3 not in locations or destination_h3 not in locations:
        raise RouteNotFoundError("The route begins or ends outside the known map.")

    start_stronghold = next(
        (stronghold for stronghold in strongholds if stronghold.location_id == start_h3),
        None,
    )
    origin_name = (
        str(origin.stronghold_name)
        if origin is not None
        else str(start_stronghold.stronghold_name)
        if start_stronghold is not None
        else "your current position"
    )
    origin_ref = (
        f"sh_{int(origin.stronghold_id)}"
        if origin is not None
        else f"sh_{int(start_stronghold.stronghold_id)}"
        if start_stronghold is not None
        else None
    )
    destination_ref = f"sh_{int(destination.stronghold_id)}"
    path, edges = _shortest_path(
        start_h3=start_h3,
        destination_h3=destination_h3,
        locations=locations,
        stronghold_cells=stronghold_cells,
        mobility=_mobility_profile(army),
        allow_off_road=allow_off_road,
    )
    route_type = "fastest" if allow_off_road else "road_only"
    if not edges:
        return {
            "origin": origin_name,
            "origin_stronghold_id": origin_ref,
            "destination": str(destination.stronghold_name),
            "destination_stronghold_id": destination_ref,
            "route_type": route_type,
            "summary": f"You are already at {destination.stronghold_name}.",
            "total_leagues": 0,
            "estimated_watches": 0,
            "legs": [],
            "initial_direction": None,
            "initial_instruction": None,
        }

    landmarks = _path_landmarks(
        path,
        origin_name=origin_name,
        origin_ref=origin_ref,
        destination=destination,
        strongholds=strongholds,
        locations=locations,
    )
    legs: list[dict[str, object]] = []
    for (start_index, start_name, start_ref), (end_index, end_name, end_ref) in zip(
        landmarks, landmarks[1:]
    ):
        if end_index <= start_index:
            continue
        segment_edges = edges[start_index:end_index]
        road_leagues = sum(edge.travel == "road" for edge in segment_edges)
        off_road_leagues = len(segment_edges) - road_leagues
        travel = "road" if not off_road_leagues else "off_road" if not road_leagues else "mixed"
        terrains: list[str] = []
        for edge in segment_edges:
            if edge.travel == "off_road" and edge.terrain not in terrains:
                terrains.append(edge.terrain)
        legs.append(
            {
                "from": start_name,
                "from_stronghold_id": start_ref,
                "to": end_name,
                "to_stronghold_id": end_ref,
                "travel": travel,
                "leagues": len(segment_edges),
                "estimated_watches": sum(edge.watches for edge in segment_edges),
                "road_leagues": road_leagues,
                "off_road_leagues": off_road_leagues,
                "terrains": terrains,
            }
        )

    phrases = [_leg_phrase(leg) for leg in legs]
    summary = f"From {origin_name}, travel {phrases[0]}"
    if len(phrases) > 1:
        summary += ", then " + ", then ".join(phrases[1:])
    summary += "."

    initial_direction = _bearing_word(path[0], path[1])
    first_landmark = legs[0]["to"] if legs else destination.stronghold_name
    if edges[0].travel == "road":
        adjective = _DIRECTION_ADJECTIVES.get(str(initial_direction), str(initial_direction or "onward"))
        initial_instruction = f"Take the {adjective} road toward {first_landmark}."
    else:
        direction = f" {initial_direction}" if initial_direction else ""
        initial_instruction = f"Travel{direction} off-road toward {first_landmark} through {edges[0].terrain.lower()}."

    return {
        "origin": origin_name,
        "origin_stronghold_id": origin_ref,
        "destination": str(destination.stronghold_name),
        "destination_stronghold_id": destination_ref,
        "route_type": route_type,
        "summary": summary,
        "total_leagues": len(edges),
        "estimated_watches": sum(edge.watches for edge in edges),
        "legs": legs,
        "initial_direction": initial_direction,
        "initial_instruction": initial_instruction,
    }
