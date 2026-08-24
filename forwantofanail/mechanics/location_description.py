from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
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


@dataclass(frozen=True)
class CommanderBriefSections:
    army: str
    time: str
    orders: str
    attention: str
    local_situation: str

    def render(self) -> str:
        sections = (
            ("ARMY", self.army),
            ("TIME", self.time),
            ("ORDERS", self.orders),
            ("ATTENTION", self.attention),
            ("LOCAL SITUATION", self.local_situation),
        )
        return "\n\n".join(
            f"{label}\n{text.strip()}"
            for label, text in sections
            if text and text.strip()
        )


def _display_army_name(value: Any) -> str:
    name = str(value or "unnamed army").strip() or "unnamed army"
    if name.casefold().startswith("the "):
        return f"the {name[4:]}"
    return f"the {name}"


def _display_calendar_date(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return raw or "an unknown date"
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _army_condition(
    *,
    army: Mapping[str, Any],
    time: Mapping[str, Any],
    environs: Mapping[str, Any],
    current_action: Mapping[str, Any] | None,
) -> str:
    kind = str((current_action or {}).get("kind") or "").strip().casefold()
    if kind == "rout":
        return "fleeing"

    watch_label = str(time.get("watch_label") or "").strip().casefold()
    column_length = float(army.get("column_length") or 0.0)
    if column_length > 2.0 and watch_label in {"night", "matin", "vesper"}:
        return "encamped"
    if watch_label == "night":
        return "encamped"
    action_conditions = {
        "move": "marching",
        "forage": "foraging",
        "attack": "attacking",
        "besiege": "besieging",
    }
    if kind in action_conditions:
        return action_conditions[kind]

    center_h3 = str(environs.get("center_h3") or "").strip()
    center_cell = _cell_mapping(environs).get(center_h3)
    stronghold = center_cell.get("stronghold") if center_cell else None
    if isinstance(stronghold, Mapping) and bool(stronghold.get("under_siege")):
        return "under siege"
    return "holding"


def _describe_own_army(army: Mapping[str, Any]) -> str:
    composition = army.get("composition") if isinstance(army.get("composition"), Mapping) else {}
    detachments = composition.get("detachments", [])
    infantry = 0
    cavalry = 0
    if isinstance(detachments, list):
        for detachment in detachments:
            if not isinstance(detachment, Mapping):
                continue
            warriors = max(0, int(detachment.get("warriors") or 0))
            if bool(detachment.get("is_cavalry")):
                cavalry += warriors
            else:
                infantry += warriors
    strength = infantry + cavalry
    opening = (
        f"Under your command is {_display_army_name(army.get('name'))}, an army {strength:,} strong "
        f"({infantry:,} infantry and {cavalry:,} cavalry)."
    )

    supply = army.get("supply") if isinstance(army.get("supply"), Mapping) else {}
    supply_current = max(0, int(supply.get("current") or 0))
    days_estimate = supply.get("days_estimate")
    if days_estimate is None:
        supply_sentence = f"You have {supply_current:,} supply; your forces currently consume no supply."
    else:
        days_remaining = max(0, int(math.floor(float(days_estimate))))
        day_unit = "day" if days_remaining == 1 else "days"
        supply_sentence = (
            f"You have {supply_current:,} supply, enough to sustain your forces for "
            f"{days_remaining:,} {day_unit}."
        )

    sentences = [opening, supply_sentence]
    morale = army.get("morale") if isinstance(army.get("morale"), Mapping) else {}
    morale_current = max(0, int(morale.get("current") or 0))
    morale_maximum = max(morale_current, int(morale.get("max") or 12))
    sentences.append(f"The army's morale is {morale_current} of {morale_maximum}.")
    column_length = max(0.0, float(army.get("column_length") or 0.0))
    if column_length >= 2.0:
        sentences.append(
            f"The army's column length is {column_length:.1f} leagues, which limits your daily march."
        )
    return " ".join(sentences)


def _describe_time(time: Mapping[str, Any], environs: Mapping[str, Any]) -> str:
    sentences: list[str] = []
    calendar_date = _display_calendar_date(time.get("calendar_date"))
    watch_label = str(time.get("watch_label") or "unknown").strip().casefold() or "unknown"
    if watch_label == "night":
        sentences.append(f"It is {calendar_date}, in the night watch.")
    else:
        ordinal = {
            "matin": "first",
            "prime": "second",
            "sixbell": "third",
            "vesper": "fourth",
        }.get(watch_label, "unknown")
        sentences.append(
            f"It is {calendar_date}, in the {watch_label} watch "
            f"({ordinal} of the four daily watches)."
        )
    radius = max(0, int(environs.get("radius") or 0))
    radius_unit = "league" if radius == 1 else "leagues"
    sentences.append(f"The army's scouting radius is {radius:,} {radius_unit} in all directions.")
    return " ".join(sentences)


def _describe_orders(
    *,
    army: Mapping[str, Any],
    time: Mapping[str, Any],
    environs: Mapping[str, Any],
    current_action: Mapping[str, Any] | None = None,
    itinerary: Mapping[str, Any] | None = None,
    standing_orders: Mapping[str, Any] | None = None,
    action_target: str = "",
    action_eta: str = "",
) -> str:
    condition = _army_condition(
        army=army,
        time=time,
        environs=environs,
        current_action=current_action,
    )
    sentences = [f"The army is currently {condition}."]
    action = current_action or {}
    kind = str(action.get("kind") or "").strip().casefold()
    state = str(action.get("state") or "").strip().casefold()
    state_phrase = "queued" if state == "queued" else "underway"
    target_suffix = f" {action_target.strip()}" if action_target.strip() else ""
    if kind == "move":
        sentences.append(f"March orders are {state_phrase}{target_suffix}.")
    elif kind == "forage":
        sentences.append(f"Foraging orders are {state_phrase}.")
    elif kind == "attack":
        sentences.append(f"An attack is {state_phrase}{target_suffix}.")
    elif kind == "besiege":
        sentences.append(f"A siege is {state_phrase}{target_suffix}.")
    elif kind == "rout":
        sentences.append(f"The army's retreat is {state_phrase}{target_suffix}.")
    else:
        sentences.append("No active orders.")
    if action_eta.strip():
        sentences.append(f"Completion is expected {action_eta.strip()}.")

    itinerary = itinerary or {}
    remaining_moves = itinerary.get("remaining_moves", [])
    remaining_rout = itinerary.get("remaining_rout", [])
    move_count = len(remaining_moves) if isinstance(remaining_moves, list) else 0
    rout_count = len(remaining_rout) if isinstance(remaining_rout, list) else 0
    stage_count = move_count or rout_count
    if stage_count:
        stage_unit = "stage remains" if stage_count == 1 else "stages remain"
        sentences.append(f"{stage_count} {stage_unit} in the present itinerary.")

    standing_orders = standing_orders or {}
    enabled: list[str] = []
    follow_road = standing_orders.get("follow_road")
    forced_march = standing_orders.get("forced_march")
    if isinstance(follow_road, Mapping) and bool(follow_road.get("enabled")):
        enabled.append("follow the road")
    if isinstance(forced_march, Mapping) and bool(forced_march.get("enabled")):
        enabled.append("forced march")
    if enabled:
        sentences.append(f"Standing orders: {_natural_join(enabled)}.")
    else:
        sentences.append("No standing orders are active.")
    return " ".join(sentences)


def _describe_attention(
    *,
    unread_letters: int,
    unread_alerts: int,
    high_importance_alerts: int,
    status_signals: Iterable[Mapping[str, Any]],
) -> str:
    letter_count = max(0, int(unread_letters))
    alert_count = max(0, int(unread_alerts))
    high_count = min(alert_count, max(0, int(high_importance_alerts)))
    letter_phrase = "no unread letters" if letter_count == 0 else (
        "1 unread letter" if letter_count == 1 else f"{letter_count:,} unread letters"
    )
    alert_phrase = "no unread alerts" if alert_count == 0 else (
        "1 unread alert" if alert_count == 1 else f"{alert_count:,} unread alerts"
    )
    sentence = f"You have {letter_phrase} and {alert_phrase}."
    if high_count:
        unit = "alert is" if high_count == 1 else "alerts are"
        sentence += f" {high_count:,} {unit} of high importance."
    signal_messages = [
        str(signal.get("message") or "").strip()
        for signal in status_signals
        if isinstance(signal, Mapping) and str(signal.get("message") or "").strip()
    ]
    if signal_messages:
        sentence += f" Current conditions: {' '.join(signal_messages)}"
    elif letter_count == 0 and alert_count == 0:
        sentence += " Nothing requires immediate attention."
    return sentence


def build_commander_brief(
    *,
    army: Mapping[str, Any],
    time: Mapping[str, Any],
    environs: Mapping[str, Any],
    current_action: Mapping[str, Any] | None = None,
    itinerary: Mapping[str, Any] | None = None,
    standing_orders: Mapping[str, Any] | None = None,
    action_target: str = "",
    action_eta: str = "",
    unread_letters: int = 0,
    unread_alerts: int = 0,
    high_importance_alerts: int = 0,
    status_signals: Iterable[Mapping[str, Any]] = (),
    border_road_cells: Iterable[str] = (),
) -> CommanderBriefSections:
    local_situation = build_environs_brief(
        environs,
        border_road_cells=border_road_cells,
    ).render()
    return CommanderBriefSections(
        army=_describe_own_army(army),
        time=_describe_time(time, environs),
        orders=_describe_orders(
            army=army,
            time=time,
            environs=environs,
            current_action=current_action,
            itinerary=itinerary,
            standing_orders=standing_orders,
            action_target=action_target,
            action_eta=action_eta,
        ),
        attention=_describe_attention(
            unread_letters=unread_letters,
            unread_alerts=unread_alerts,
            high_importance_alerts=high_importance_alerts,
            status_signals=status_signals,
        ),
        local_situation=local_situation,
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
        if bool(current_stronghold.get("under_siege")):
            siege = current_stronghold.get("siege")
            besieger = (
                str(siege.get("besieger_faction") or "").strip()
                if isinstance(siege, Mapping)
                else ""
            )
            section += (
                f" The stronghold is under siege by {besieger} forces."
                if besieger
                else " The stronghold is under siege."
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
        siege_phrase = ""
        if bool(stronghold.get("under_siege")):
            siege = stronghold.get("siege")
            besieger = (
                str(siege.get("besieger_faction") or "").strip()
                if isinstance(siege, Mapping)
                else ""
            )
            siege_phrase = f", under siege by {besieger}" if besieger else ", under siege"
        phrase = (
            f"{stronghold_type} of {name} {position}, controlled by {faction}"
            f"{siege_phrase} (garrison {garrison})"
        )
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
        return "No other armies are nearby."
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
