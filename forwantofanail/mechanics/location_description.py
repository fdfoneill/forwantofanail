from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from typing import Any

import h3
from sqlalchemy.orm import Session

from forwantofanail.core.models import Location, Stronghold
from forwantofanail.mechanics.forage import forage_condition_word, forage_depletion_level


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
COMPASS_ORDER = {direction: index for index, direction in enumerate(COMPASS_DIRECTIONS)}


@dataclass(frozen=True)
class EnvironsBriefSections:
    terrain: str = ""
    forage: str = ""
    roads: str = ""
    strongholds: str = ""
    armies: str = ""

    def render(self) -> str:
        return " ".join(
            section.strip()
            for section in (self.terrain, self.forage, self.roads, self.strongholds, self.armies)
            if section and section.strip()
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


def _bearing_word_from_latlng(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
) -> str:
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


def _bearing_word(origin_h3: str, destination_h3: str) -> str | None:
    """Return the eight-point bearing from origin to destination."""
    try:
        origin_lat, origin_lng = _cell_latlng(origin_h3)
        destination_lat, destination_lng = _cell_latlng(destination_h3)
    except Exception:
        return None
    return _bearing_word_from_latlng(origin_lat, origin_lng, destination_lat, destination_lng)


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


def _h3_neighbors(cell_h3: str) -> set[str]:
    try:
        return {str(neighbor) for neighbor in h3.grid_ring(cell_h3, 1)}
    except Exception:
        return set()


def _connected_components(cell_ids: Iterable[str]) -> list[set[str]]:
    remaining = {str(cell_id) for cell_id in cell_ids}
    components: list[set[str]] = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component = {seed}
        queue = deque([seed])
        while queue:
            cell_h3 = queue.popleft()
            for neighbor_h3 in sorted(_h3_neighbors(cell_h3) & remaining):
                remaining.remove(neighbor_h3)
                component.add(neighbor_h3)
                queue.append(neighbor_h3)
        components.append(component)
    return components


def _component_bearing(center_h3: str, component: Iterable[str]) -> str | None:
    points: list[tuple[float, float]] = []
    for cell_h3 in component:
        try:
            points.append(_cell_latlng(cell_h3))
        except Exception:
            continue
    if not points:
        return None
    try:
        center_lat, center_lng = _cell_latlng(center_h3)
    except Exception:
        return None
    average_lat = sum(point[0] for point in points) / len(points)
    average_lng = sum(point[1] for point in points) / len(points)
    return _bearing_word_from_latlng(center_lat, center_lng, average_lat, average_lng)


def _natural_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _inline_proper_name(value: Any, fallback: str) -> str:
    name = str(value or fallback).strip() or fallback
    if name.startswith("The "):
        return f"the {name[4:]}"
    return name


def _cell_mapping(environs: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    cells = environs.get("cells", [])
    if not isinstance(cells, list):
        return {}
    return {
        str(cell.get("h3")): cell
        for cell in cells
        if isinstance(cell, Mapping) and str(cell.get("h3") or "").strip()
    }


def _describe_terrain_section(
    *,
    center_h3: str,
    cells_by_h3: Mapping[str, Mapping[str, Any]],
) -> str:
    center_cell = cells_by_h3.get(center_h3)
    if center_cell is None:
        return ""
    current_terrain = str(center_cell.get("terrain_type") or "unknown").strip() or "unknown"
    current_terrain_display = current_terrain.lower()
    terrain_cells: dict[str, tuple[str, set[str]]] = {}
    for cell_h3, cell in cells_by_h3.items():
        terrain_name = str(cell.get("terrain_type") or "unknown").strip() or "unknown"
        normalized = terrain_name.casefold()
        if normalized == current_terrain.casefold():
            continue
        display_name, cell_ids = terrain_cells.setdefault(normalized, (terrain_name, set()))
        cell_ids.add(cell_h3)
        terrain_cells[normalized] = (display_name, cell_ids)

    features: list[tuple[int, str, str, str]] = []
    for terrain_name, cell_ids in terrain_cells.values():
        for component in _connected_components(cell_ids):
            bearing = _component_bearing(center_h3, component)
            if bearing is None:
                continue
            phrase = f"{terrain_name.lower()} to the {bearing}"
            features.append((COMPASS_ORDER[bearing], terrain_name.casefold(), min(component), phrase))

    if not bool(center_cell.get("has_road")):
        road_cells = {
            cell_h3
            for cell_h3, cell in cells_by_h3.items()
            if bool(cell.get("has_road"))
        }
        def distance_key(cell_h3: str) -> tuple[float, str]:
            distance = _grid_distance(center_h3, cell_h3)
            return (float(distance) if distance is not None else math.inf, cell_h3)

        for component in _connected_components(road_cells):
            ranked = sorted(
                component,
                key=distance_key,
            )
            if not ranked:
                continue
            bearing = _bearing_word(center_h3, ranked[0])
            if bearing is None:
                continue
            features.append((COMPASS_ORDER[bearing], "road", min(component), f"a road to the {bearing}"))

    current_stronghold = (
        center_cell.get("stronghold")
        if isinstance(center_cell.get("stronghold"), Mapping)
        else None
    )
    if current_stronghold is not None:
        stronghold_type = (
            str(current_stronghold.get("type") or "stronghold").strip().lower()
            or "stronghold"
        )
        stronghold_name = _inline_proper_name(
            current_stronghold.get("name"),
            "unknown stronghold",
        )
        section = (
            f"The army is occupying the {stronghold_type} of {stronghold_name}, "
            f"in {current_terrain_display} terrain."
        )
    elif bool(center_cell.get("has_road")):
        section = f"The army is on the road in {current_terrain_display} terrain."
    else:
        section = f"The army is in {current_terrain_display} terrain."
    if features:
        phrases = [row[3] for row in sorted(features)]
        section += f" Nearby terrain includes {_natural_join(phrases)}."
    return section


def _describe_forage_section(cells_by_h3: Mapping[str, Mapping[str, Any]]) -> str:
    forageable = [
        cell
        for cell in cells_by_h3.values()
        if int(cell.get("settlement") or 0) > 0
    ]
    average_depletion = (
        sum(forage_depletion_level(cell.get("foraged_this_season")) for cell in forageable)
        / len(forageable)
        if forageable
        else 3.0
    )
    return f"The area is {forage_condition_word(average_depletion)} in terms of forage."


def _visible_strongholds(
    cells_by_h3: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {
        cell_h3: cell["stronghold"]
        for cell_h3, cell in cells_by_h3.items()
        if isinstance(cell.get("stronghold"), Mapping)
    }


def _strongholds_reached_by_road_cell(
    *,
    road_cells: set[str],
    strongholds_by_h3: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[tuple[str, Mapping[str, Any]]]]:
    reached: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for stronghold_h3, stronghold in strongholds_by_h3.items():
        for road_h3 in road_cells:
            distance = _grid_distance(road_h3, stronghold_h3)
            if distance in {0, 1}:
                reached.setdefault(road_h3, []).append((stronghold_h3, stronghold))
    for rows in reached.values():
        rows.sort(key=lambda row: (str(row[1].get("id") or ""), row[0]))
    return reached


def _describe_road_section(
    *,
    center_h3: str,
    cells_by_h3: Mapping[str, Mapping[str, Any]],
    border_road_cells: set[str],
) -> str:
    center_cell = cells_by_h3.get(center_h3)
    if center_cell is None or not bool(center_cell.get("has_road")):
        return ""
    road_cells = {
        cell_h3
        for cell_h3, cell in cells_by_h3.items()
        if bool(cell.get("has_road"))
    }
    starts = sorted(_h3_neighbors(center_h3) & road_cells)
    if not starts:
        return "The road ends here."

    reached_by_cell = _strongholds_reached_by_road_cell(
        road_cells=road_cells,
        strongholds_by_h3={
            stronghold_h3: stronghold
            for stronghold_h3, stronghold in _visible_strongholds(cells_by_h3).items()
            if stronghold_h3 != center_h3
        },
    )
    terminals: dict[tuple[str, str], tuple[int, str, str, str]] = {}
    for start_h3 in starts:
        queue: deque[tuple[str, int]] = deque([(start_h3, 1)])
        visited = {center_h3, start_h3}
        while queue:
            cell_h3, distance = queue.popleft()
            reached = reached_by_cell.get(cell_h3, [])
            if reached:
                stronghold_h3, stronghold = reached[0]
                key = ("stronghold", str(stronghold.get("id") or stronghold_h3))
                candidate = (
                    distance,
                    start_h3,
                    "stronghold",
                    _inline_proper_name(stronghold.get("name"), "unknown stronghold"),
                )
                if key not in terminals or candidate[:2] < terminals[key][:2]:
                    terminals[key] = candidate
                continue

            outbound = sorted(_h3_neighbors(cell_h3) & border_road_cells)
            for outbound_h3 in outbound:
                key = ("exit", outbound_h3)
                candidate = (distance + 1, start_h3, "exit", outbound_h3)
                if key not in terminals or candidate[:2] < terminals[key][:2]:
                    terminals[key] = candidate

            road_neighbors = (_h3_neighbors(cell_h3) & road_cells) - {center_h3}
            onward = sorted(road_neighbors - visited)
            if not onward and not outbound:
                if len(road_neighbors) <= 1:
                    key = ("dead-end", cell_h3)
                    candidate = (distance, start_h3, "dead-end", cell_h3)
                    if key not in terminals or candidate[:2] < terminals[key][:2]:
                        terminals[key] = candidate
            for neighbor_h3 in onward:
                visited.add(neighbor_h3)
                queue.append((neighbor_h3, distance + 1))

    clauses: dict[str, tuple[int, str]] = {}
    for _distance, start_h3, terminal_kind, label in terminals.values():
        bearing = _bearing_word(center_h3, start_h3)
        if bearing is None:
            continue
        if terminal_kind == "stronghold":
            clause = f"{bearing} towards {label}"
        elif terminal_kind == "dead-end":
            clause = f"{bearing} to a dead end"
        else:
            clause = bearing
        clauses.setdefault(clause, (COMPASS_ORDER[bearing], clause))
    if not clauses:
        return "The road loops through the area."
    ordered = [row[1] for row in sorted(clauses.values())]
    return f"The road leads {_natural_join(ordered)}."


def _describe_strongholds_section(
    *,
    center_h3: str,
    cells_by_h3: Mapping[str, Mapping[str, Any]],
) -> str:
    rows: list[tuple[int, str, str]] = []
    for stronghold_h3, stronghold in _visible_strongholds(cells_by_h3).items():
        if stronghold_h3 == center_h3:
            continue
        distance = 0 if stronghold_h3 == center_h3 else _grid_distance(center_h3, stronghold_h3)
        if distance is None:
            continue
        stronghold_type = str(stronghold.get("type") or "stronghold").strip().lower() or "stronghold"
        name = _inline_proper_name(stronghold.get("name"), "unknown stronghold")
        faction = str(stronghold.get("faction") or "unknown").strip() or "unknown"
        garrison = max(0, int(stronghold.get("defender_strength") or 0))
        if distance == 0:
            position = "here"
        else:
            bearing = _bearing_word(center_h3, stronghold_h3)
            if bearing is None:
                continue
            unit = "league" if distance == 1 else "leagues"
            position = f"{distance} {unit} to the {bearing}"
        phrase = f"{stronghold_type} of {name} {position}, controlled by {faction} (garrison {garrison})"
        rows.append((distance, str(stronghold.get("id") or stronghold_h3), phrase))
    if not rows:
        return ""
    return f"Nearby strongholds: {'; '.join(row[2] for row in sorted(rows))}."


def _visible_army_strength(army: Mapping[str, Any]) -> int | None:
    if "infantry" in army or "cavalry" in army:
        return max(0, int(army.get("infantry") or 0)) + max(0, int(army.get("cavalry") or 0))
    if army.get("strength_rounded") is not None:
        return max(0, int(army["strength_rounded"]))
    return None


def _describe_armies_section(
    *,
    center_h3: str,
    cells_by_h3: Mapping[str, Mapping[str, Any]],
) -> str:
    rows: list[tuple[int, str, str]] = []
    for cell_h3, cell in cells_by_h3.items():
        other_armies = cell.get("other_armies", [])
        if not isinstance(other_armies, list):
            continue
        distance = 0 if cell_h3 == center_h3 else _grid_distance(center_h3, cell_h3)
        if distance is None:
            continue
        stronghold = cell.get("stronghold") if isinstance(cell.get("stronghold"), Mapping) else None
        for army in other_armies:
            if not isinstance(army, Mapping):
                continue
            faction = str(army.get("faction") or "unknown").strip() or "unknown"
            phrase = f"{faction} army"
            name = str(army.get("name") or "").strip()
            if name:
                phrase += f' "{name.replace(chr(34), chr(39))}"'
            strength = _visible_army_strength(army)
            commander = str(army.get("commander") or "").strip()
            details: list[str] = []
            if strength is not None:
                details.append(f"strength {strength:,}")
            if commander:
                details.append(f"commanded by {commander}")
            if details:
                phrase += f" ({', '.join(details)})"
            if stronghold is not None:
                phrase += f" occupying {_inline_proper_name(stronghold.get('name'), 'unknown stronghold')}"
            elif bool(cell.get("has_road")):
                phrase += " on the road"
            else:
                terrain_name = str(cell.get("terrain_type") or "unknown").strip().lower() or "unknown"
                phrase += f" in {terrain_name} terrain"

            if stronghold is None:
                if distance == 0:
                    phrase += " here"
                else:
                    bearing = _bearing_word(center_h3, cell_h3)
                    if bearing is None:
                        continue
                    unit = "league" if distance == 1 else "leagues"
                    phrase += f" {distance} {unit} to the {bearing}"
            rows.append((distance, str(army.get("army_id") or ""), phrase))
    if not rows:
        return ""
    return f"Other armies: {'; '.join(row[2] for row in sorted(rows))}."


def build_environs_brief(
    environs: Mapping[str, Any],
    *,
    border_road_cells: Iterable[str] = (),
) -> EnvironsBriefSections:
    center_h3 = str(environs.get("center_h3") or "").strip()
    cells_by_h3 = _cell_mapping(environs)
    if not center_h3 or center_h3 not in cells_by_h3:
        return EnvironsBriefSections()
    return EnvironsBriefSections(
        terrain=_describe_terrain_section(center_h3=center_h3, cells_by_h3=cells_by_h3),
        forage=_describe_forage_section(cells_by_h3),
        roads=_describe_road_section(
            center_h3=center_h3,
            cells_by_h3=cells_by_h3,
            border_road_cells={str(cell_h3) for cell_h3 in border_road_cells},
        ),
        strongholds=_describe_strongholds_section(center_h3=center_h3, cells_by_h3=cells_by_h3),
        armies=_describe_armies_section(center_h3=center_h3, cells_by_h3=cells_by_h3),
    )
