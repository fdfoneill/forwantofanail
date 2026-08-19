from __future__ import annotations

from collections import deque
import math

import h3
from sqlalchemy.orm import Session

from forwantofanail.core.models import Location, Stronghold


COMPASS_DIRECTIONS = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)


def _grid_distance(origin_h3: str, destination_h3: str) -> int | None:
    try:
        if hasattr(h3, "grid_distance"):
            return int(h3.grid_distance(origin_h3, destination_h3))
        if hasattr(h3, "h3_distance"):
            return int(h3.h3_distance(origin_h3, destination_h3))
    except Exception:
        return None
    return None


def _cell_latlng(cell_h3: str) -> tuple[float, float]:
    if hasattr(h3, "cell_to_latlng"):
        lat, lng = h3.cell_to_latlng(cell_h3)
    else:
        lat, lng = h3.h3_to_geo(cell_h3)
    return float(lat), float(lng)


def _bearing_word(origin_h3: str, destination_h3: str) -> str | None:
    """Return the eight-point bearing from origin to destination."""
    try:
        origin_lat, origin_lng = _cell_latlng(origin_h3)
        destination_lat, destination_lng = _cell_latlng(destination_h3)
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
    return COMPASS_DIRECTIONS[int((bearing + 22.5) // 45.0) % len(COMPASS_DIRECTIONS)]


def _road_neighbors(cell_h3: str, road_cells: set[str]) -> list[str]:
    try:
        adjacent = h3.grid_ring(cell_h3, 1)
    except Exception:
        return []
    return sorted(str(neighbor) for neighbor in adjacent if neighbor in road_cells)


def _nearest_stronghold_on_road_branch(
    *,
    origin_h3: str,
    branch_h3: str,
    road_cells: set[str],
    strongholds_by_road_cell: dict[str, list[Stronghold]],
) -> tuple[int, Stronghold] | None:
    queue: deque[tuple[str, int]] = deque([(branch_h3, 1)])
    visited = {origin_h3, branch_h3}
    nearest_distance: int | None = None
    nearest_strongholds: list[Stronghold] = []
    while queue:
        cell_h3, distance = queue.popleft()
        if nearest_distance is not None and distance > nearest_distance:
            break
        reached = strongholds_by_road_cell.get(cell_h3, [])
        if reached:
            nearest_distance = distance
            nearest_strongholds.extend(reached)
            continue
        for neighbor_h3 in _road_neighbors(cell_h3, road_cells):
            if neighbor_h3 in visited:
                continue
            visited.add(neighbor_h3)
            queue.append((neighbor_h3, distance + 1))
    if nearest_distance is None:
        return None
    return nearest_distance, min(nearest_strongholds, key=lambda row: int(row.stronghold_id))


def _road_stronghold_pair(
    session: Session,
    *,
    location_h3: str,
    strongholds: list[Stronghold],
) -> tuple[Stronghold, Stronghold] | None:
    road_cells = {
        str(row[0])
        for row in session.query(Location.location_id)
        .filter(Location.is_road.is_(True))
        .all()
    }
    if location_h3 not in road_cells:
        return None

    strongholds_by_road_cell: dict[str, list[Stronghold]] = {}
    for stronghold in strongholds:
        reachable_cells = {str(stronghold.location_id)}
        try:
            reachable_cells.update(str(cell_h3) for cell_h3 in h3.grid_ring(stronghold.location_id, 1))
        except Exception:
            pass
        for cell_h3 in reachable_cells & road_cells:
            strongholds_by_road_cell.setdefault(cell_h3, []).append(stronghold)
    reached: dict[int, tuple[int, Stronghold]] = {}
    for branch_h3 in _road_neighbors(location_h3, road_cells):
        result = _nearest_stronghold_on_road_branch(
            origin_h3=location_h3,
            branch_h3=branch_h3,
            road_cells=road_cells,
            strongholds_by_road_cell=strongholds_by_road_cell,
        )
        if result is None:
            continue
        distance, stronghold = result
        existing = reached.get(int(stronghold.stronghold_id))
        if existing is None or distance < existing[0]:
            reached[int(stronghold.stronghold_id)] = result

    ranked = sorted(reached.values(), key=lambda row: (row[0], int(row[1].stronghold_id)))
    if len(ranked) < 2:
        return None
    return ranked[0][1], ranked[1][1]


def describe_army_location(session: Session, location_h3: str) -> str:
    """Describe a cell using occupation, adjacency, roads, then bearing and distance."""
    strongholds = session.query(Stronghold).order_by(Stronghold.stronghold_id.asc()).all()
    if not strongholds:
        return "at an unknown location"

    occupying = next(
        (stronghold for stronghold in strongholds if str(stronghold.location_id) == location_h3),
        None,
    )
    if occupying is not None:
        return f"occupying {occupying.stronghold_name}"

    ranked: list[tuple[int, Stronghold]] = []
    for stronghold in strongholds:
        distance = _grid_distance(str(stronghold.location_id), location_h3)
        if distance is None or distance <= 0:
            continue
        ranked.append((distance, stronghold))
    ranked.sort(key=lambda row: (row[0], int(row[1].stronghold_id)))

    if ranked and ranked[0][0] == 1:
        return f"outside {ranked[0][1].stronghold_name}"

    road_pair = _road_stronghold_pair(
        session,
        location_h3=location_h3,
        strongholds=strongholds,
    )
    if road_pair is not None:
        return (
            f"on the road between {road_pair[0].stronghold_name} "
            f"and {road_pair[1].stronghold_name}"
        )

    for _distance, stronghold in ranked:
        bearing = _bearing_word(str(stronghold.location_id), location_h3)
        if bearing is None:
            continue
        return f"{bearing} of {stronghold.stronghold_name}"
    return "at an unknown location"
