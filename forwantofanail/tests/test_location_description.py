from __future__ import annotations

from itertools import combinations
from types import SimpleNamespace

import h3
import pytest

from forwantofanail.api import routes
from forwantofanail.core.database import create_session, reset_database_runtime
from forwantofanail.core.migrate_runtime_tables import migrate_runtime_tables
from forwantofanail.core.models import Location, Stronghold, TerrainType
from forwantofanail.mechanics import location_description
from forwantofanail.mechanics.location_description import (
    build_commander_brief,
    build_environs_brief,
    describe_army_location,
    describe_march_stage,
    describe_route_endpoint,
    morale_condition_word,
)


@pytest.fixture()
def location_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'locations.db'}")
    monkeypatch.setenv("ADMIN_TOKEN", "location-test-admin")
    reset_database_runtime()
    migrate_runtime_tables()
    session = create_session()
    session.add(
        TerrainType(
            terrain_id=1,
            terrain_name="Open Ground",
            speed_multiplier=1.0,
            scout_multiplier=1.0,
            is_water=False,
        )
    )
    session.flush()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        reset_database_runtime()


def _add_location(session, cell_h3: str, *, is_road: bool = False) -> None:
    session.add(
        Location(
            location_id=cell_h3,
            terrain_id=1,
            region=None,
            is_road=is_road,
            settlement=0,
        )
    )


def _add_stronghold(session, stronghold_id: int, name: str, cell_h3: str) -> None:
    session.add(
        Stronghold(
            stronghold_id=stronghold_id,
            stronghold_name=name,
            stronghold_type="fortress",
            location_id=cell_h3,
            control="Allakia",
            stronghold_threshold=0,
        )
    )


def test_location_description_prefers_occupation_then_adjacency(location_session):
    stronghold_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    adjacent_h3 = sorted(h3.grid_ring(stronghold_h3, 1))[0]
    _add_location(location_session, stronghold_h3)
    _add_location(location_session, adjacent_h3)
    _add_stronghold(location_session, 1, "Highhold", stronghold_h3)
    location_session.flush()

    assert describe_army_location(location_session, stronghold_h3) == "occupying Highhold"
    assert describe_army_location(location_session, adjacent_h3) == "outside Highhold"


def test_location_description_traces_distinct_road_directions(location_session):
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    ring = sorted(h3.grid_ring(center_h3, 3))
    path = None
    for first_h3, second_h3 in combinations(ring, 2):
        candidate = list(h3.grid_path_cells(first_h3, second_h3))
        if center_h3 in candidate and len(candidate) >= 7:
            path = candidate
            break
    assert path is not None

    _add_location(location_session, path[0])
    _add_location(location_session, path[-1])
    for cell_h3 in path[1:-1]:
        _add_location(location_session, cell_h3, is_road=True)
    _add_stronghold(location_session, 1, "Westgate", path[0])
    _add_stronghold(location_session, 2, "Eastgate", path[-1])
    location_session.flush()

    assert describe_army_location(location_session, center_h3) == "on the road between Westgate and Eastgate"


def test_location_description_falls_back_to_distance_and_bearing(location_session):
    stronghold_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    army_h3 = sorted(h3.grid_ring(stronghold_h3, 3))[0]
    _add_location(location_session, stronghold_h3)
    _add_location(location_session, army_h3)
    _add_stronghold(location_session, 1, "Highhold", stronghold_h3)
    location_session.flush()

    bearing = location_description._bearing_word(stronghold_h3, army_h3)
    assert describe_army_location(location_session, army_h3) == f"{bearing} of Highhold"


def test_invalid_h3_distance_is_not_treated_as_zero(location_session, monkeypatch):
    stronghold_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    army_h3 = sorted(h3.grid_ring(stronghold_h3, 3))[0]
    _add_location(location_session, stronghold_h3)
    _add_location(location_session, army_h3)
    _add_stronghold(location_session, 1, "Highhold", stronghold_h3)
    location_session.flush()

    monkeypatch.setattr(location_description.h3, "grid_distance", lambda *_args: (_ for _ in ()).throw(ValueError()))

    assert describe_army_location(location_session, army_h3) == "at an unknown location"


def test_sympathizer_letter_uses_hierarchical_location_description(monkeypatch):
    army = SimpleNamespace(
        army_id=1,
        army_name="The Host",
        commander_id=1,
        location_id="army-cell",
        army_morale=1,
        is_garrison=False,
        detachments=[],
    )
    recipient_army = SimpleNamespace(commander_id=2, location_id="recipient-cell")
    message: dict[str, object] = {}
    rolls = iter((4, 5))

    monkeypatch.setattr(routes.random, "randint", lambda *_args: next(rolls))
    monkeypatch.setattr(routes, "_nearest_other_commander_army", lambda *_args: recipient_army)
    monkeypatch.setattr(routes, "_commander_display_name", lambda _commander: "Recipient")
    monkeypatch.setattr(routes, "describe_army_location", lambda *_args: "outside Highhold")
    monkeypatch.setattr(routes, "_create_message", lambda _session, **kwargs: message.update(kwargs))
    monkeypatch.setattr(routes, "_create_alert", lambda *_args, **_kwargs: None)
    session = SimpleNamespace(get=lambda *_args: SimpleNamespace(commander_id=2))

    routes._run_morale_test_for_army(
        session,
        army=army,
        clock=SimpleNamespace(day=1, watch=1),
        category="test",
    )

    assert message["content"] == "I write from within the ranks. The army is currently outside Highhold."


def _cell_at_bearing(center_h3: str, distance: int, bearing: str) -> str:
    return next(
        cell_h3
        for cell_h3 in sorted(h3.grid_ring(center_h3, distance))
        if location_description._bearing_word(center_h3, cell_h3) == bearing
    )


def _brief_cell(
    cell_h3: str,
    *,
    terrain: str = "Open Ground",
    road: bool = False,
    settlement: int = 1,
    forage: int = 0,
    stronghold=None,
    armies=None,
):
    return {
        "h3": cell_h3,
        "terrain_type": terrain,
        "has_road": road,
        "settlement": settlement,
        "foraged_this_season": forage,
        "stronghold": stronghold,
        "other_armies": armies or [],
    }


@pytest.mark.parametrize(
    ("morale", "expected"),
    [
        (2, "abysmal"),
        (3, "terrible"),
        (4, "terrible"),
        (5, "poor"),
        (6, "poor"),
        (7, "fair"),
        (8, "fair"),
        (9, "good"),
        (10, "soaring"),
        (11, "soaring"),
        (12, "soaring"),
    ],
)
def test_morale_condition_word_matches_dashboard_icon_buckets(morale, expected):
    assert morale_condition_word(morale) == expected


def test_commander_brief_uses_labeled_diegetic_sections():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    army = {
        "name": "The Blessed Banners",
        "composition": {
            "detachments": [
                {"warriors": 2500, "is_cavalry": False},
                {"warriors": 500, "is_cavalry": True},
            ],
        },
        "supply": {"current": 12345, "days_estimate": 7.9},
        "column_length": 2.25,
        "morale": {"current": 9, "max": 12},
    }
    time = {"calendar_date": "1410-08-24", "watch_label": "prime"}
    environs = {
        "center_h3": center_h3,
        "radius": 3,
        "cells": [_brief_cell(center_h3)],
    }

    brief = build_commander_brief(
        army=army,
        time=time,
        environs=environs,
        current_action={"kind": "besiege", "state": "in_progress"},
        itinerary={"remaining_moves": [], "remaining_rout": []},
        standing_orders={
            "follow_road": {"enabled": True},
            "forced_march": {"enabled": True},
        },
        action_target="against Bemm",
        action_eta="during the vesper watch on August 24, 1410",
        unread_letters=2,
        unread_alerts=3,
        high_importance_alerts=1,
        status_signals=[{"message": "Enemy forces are nearby."}],
    ).render()

    assert brief.split("\n\n") == [
        "ARMY\nUnder your command is the Blessed Banners, an army 3,000 strong "
        "(2,500 infantry and 500 cavalry). You have 12,345 supply, enough to sustain "
        "your forces for 7 days. The army's morale is good. The army's column length "
        "is 2.2 leagues, which limits your daily march.",
        "TIME\nIt is August 24, 1410, in the prime watch (second of the four daily watches). "
        "The army's scouting radius is 3 leagues in all directions.",
        "ORDERS\nThe army is currently besieging. A siege is underway against Bemm. "
        "Completion is expected during the vesper watch on August 24, 1410. "
        "Standing orders: follow the road and forced march.",
        "ATTENTION\nYou have 2 unread letters and 3 unread alerts. 1 alert is of high importance. "
        "Current conditions: Enemy forces are nearby.",
        "LOCAL SITUATION\nThe army is in open ground terrain. The area is untouched in terms of forage. "
        "No other armies are nearby.",
    ]


def test_commander_brief_uses_night_wording_and_dashboard_condition_precedence():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    army = {
        "name": "Night Guard",
        "composition": {"detachments": [{"warriors": 1, "is_cavalry": False}]},
        "supply": {"current": 0, "days_estimate": None},
        "column_length": 0.5,
        "morale": {"current": 8, "max": 12},
    }
    time = {"calendar_date": "1410-08-24", "watch_label": "night"}
    environs = {
        "center_h3": center_h3,
        "radius": 1,
        "cells": [_brief_cell(center_h3)],
    }

    brief = build_commander_brief(army=army, time=time, environs=environs).render()

    assert "your forces currently consume no supply" in brief
    assert "The army is currently encamped." in brief
    assert "It is August 24, 1410, in the night watch." in brief
    assert "first of the four daily watches" not in brief
    assert "scouting radius is 1 league" in brief


def test_commander_brief_does_not_call_unread_items_nothing_to_attend_to():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    brief = build_commander_brief(
        army={
            "name": "Watchful Host",
            "composition": {"detachments": []},
            "supply": {"current": 0, "days_estimate": None},
            "morale": {"current": 9, "max": 12},
        },
        time={"calendar_date": "1410-08-24", "watch_label": "matin"},
        environs={
            "center_h3": center_h3,
            "radius": 2,
            "cells": [_brief_cell(center_h3)],
        },
        unread_letters=1,
    ).render()

    assert "You have 1 unread letter and no unread alerts." in brief
    assert "Nothing requires immediate attention" not in brief


@pytest.mark.parametrize(
    ("kind", "condition", "order_text"),
    [
        ("move", "marching", "Its next stage leads toward Bemm."),
        ("forage", "foraging", "Foraging orders are underway."),
        ("attack", "attacking", "An attack is underway against the Blessed Banners."),
        ("besiege", "besieging", "A siege is underway against Bemm."),
        ("rout", "fleeing", "The army's retreat is underway."),
    ],
)
def test_commander_brief_describes_each_action_kind(kind, condition, order_text):
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    target = {
        "move": "toward Bemm",
        "attack": "against the Blessed Banners",
        "besiege": "against Bemm",
    }.get(kind, "")
    brief = build_commander_brief(
        army={
            "name": "Test Host",
            "composition": {"detachments": []},
            "supply": {"current": 0, "days_estimate": None},
            "morale": {"current": 9, "max": 12},
        },
        time={"calendar_date": "1410-08-24", "watch_label": "prime"},
        environs={
            "center_h3": center_h3,
            "radius": 2,
            "cells": [_brief_cell(center_h3)],
        },
        current_action={"kind": kind, "state": "in_progress"},
        action_target=target,
    ).render()

    assert f"The army is currently {condition}." in brief
    assert order_text in brief


def test_commander_brief_distinguishes_immediate_stage_from_route_endpoint():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    brief = build_commander_brief(
        army={
            "name": "Road Host",
            "composition": {"detachments": []},
            "supply": {"current": 0, "days_estimate": None},
            "morale": {"current": 9, "max": 12},
        },
        time={"calendar_date": "1410-08-24", "watch_label": "prime"},
        environs={
            "center_h3": center_h3,
            "radius": 2,
            "cells": [_brief_cell(center_h3)],
        },
        current_action={"kind": "move", "state": "in_progress"},
        itinerary={"remaining_moves": ["stage_1", "stage_2", "stage_3"]},
        action_target="southwest along the road between Castle Nalish and Yinnagul",
        route_endpoint="west of Yinnagul",
    ).render()

    assert (
        "Its next stage leads southwest along the road between Castle Nalish and Yinnagul."
        in brief
    )
    assert "3 stages remain in the ordered route, which ends west of Yinnagul." in brief


def test_march_stage_adds_bearing_and_distinguishes_onto_from_along_road(monkeypatch):
    origin_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    destination_h3 = _cell_at_bearing(origin_h3, 1, "southwest")
    monkeypatch.setattr(
        location_description,
        "describe_army_location",
        lambda _session, _location: "on the road between Castle Nalish and Yinnagul",
    )

    class RoadSession:
        def __init__(self, origin_is_road):
            self.origin_is_road = origin_is_road

        def get(self, model, key):
            assert model is Location and key == origin_h3
            return SimpleNamespace(is_road=self.origin_is_road)

    assert describe_march_stage(RoadSession(False), origin_h3, destination_h3) == (
        "southwest onto the road between Castle Nalish and Yinnagul"
    )
    assert describe_march_stage(RoadSession(True), origin_h3, destination_h3) == (
        "southwest along the road between Castle Nalish and Yinnagul"
    )


def test_route_endpoint_uses_diegetic_location_without_cell_identifier(monkeypatch):
    monkeypatch.setattr(
        location_description,
        "describe_army_location",
        lambda _session, _location: "west of Yinnagul",
    )

    endpoint = describe_route_endpoint(SimpleNamespace(), "8f6889082cc820c")

    assert endpoint == "west of Yinnagul"
    assert "8f6889082cc820c" not in endpoint


def test_environs_brief_describes_discrete_terrain_forage_and_offroad_roads():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    north_h3 = _cell_at_bearing(center_h3, 2, "north")
    west_h3 = _cell_at_bearing(center_h3, 2, "west")
    southeast_h3 = _cell_at_bearing(center_h3, 2, "southeast")
    environs = {
        "center_h3": center_h3,
        "radius": 2,
        "cells": [
            _brief_cell(center_h3, forage=0),
            _brief_cell(north_h3, terrain="Desert", forage=0),
            _brief_cell(west_h3, terrain="River", road=True, forage=1),
            _brief_cell(southeast_h3, terrain="River", forage=1),
        ],
    }

    sections = build_environs_brief(environs)

    assert sections.terrain.startswith("The army is in open ground terrain.")
    assert "desert to the north" in sections.terrain
    assert "river to the west" in sections.terrain
    assert "river to the southeast" in sections.terrain
    assert "a road to the west" in sections.terrain
    assert sections.forage == "The area is plentiful in terms of forage."
    assert sections.roads == ""
    assert sections.armies == "No other armies are nearby."


def test_environs_brief_names_visible_strongholds_adjacent_to_road_ends():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    ring = sorted(h3.grid_ring(center_h3, 3))
    path = None
    for first_h3, second_h3 in combinations(ring, 2):
        candidate = list(h3.grid_path_cells(first_h3, second_h3))
        if center_h3 in candidate and len(candidate) >= 7:
            path = candidate
            break
    assert path is not None
    cells = [_brief_cell(cell_h3, road=True) for cell_h3 in path[1:-1]]
    cells.extend(
        [
            _brief_cell(
                path[0],
                stronghold={
                    "id": "sh_1",
                    "name": "Westgate",
                    "type": "fortress",
                    "faction": "Allakia",
                    "defender_strength": 100,
                },
            ),
            _brief_cell(
                path[-1],
                stronghold={
                    "id": "sh_2",
                    "name": "Eastgate",
                    "type": "town",
                    "faction": "Dinn",
                    "defender_strength": 50,
                },
            ),
        ]
    )

    sections = build_environs_brief({"center_h3": center_h3, "radius": 3, "cells": cells})

    assert sections.roads.startswith("The road leads ")
    assert "towards Westgate" in sections.roads
    assert "towards Eastgate" in sections.roads
    assert "dead end" not in sections.roads


def test_environs_road_does_not_lead_towards_center_stronghold():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    road_h3 = sorted(h3.grid_ring(center_h3, 1))[0]
    environs = {
        "center_h3": center_h3,
        "radius": 1,
        "cells": [
            _brief_cell(
                center_h3,
                road=True,
                stronghold={
                    "id": "sh_1",
                    "name": "Centerhold",
                    "type": "town",
                    "faction": "Boonan",
                    "defender_strength": 0,
                },
            ),
            _brief_cell(road_h3, road=True),
        ],
    }

    section = build_environs_brief(environs).roads

    assert section.endswith("to a dead end.")
    assert "towards Centerhold" not in section


def test_environs_brief_opens_with_occupied_stronghold_and_omits_it_from_nearby_list():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    nearby_h3 = sorted(h3.grid_ring(center_h3, 1))[0]
    environs = {
        "center_h3": center_h3,
        "radius": 1,
        "cells": [
            _brief_cell(
                center_h3,
                terrain="Open Ground",
                road=True,
                stronghold={
                    "id": "sh_center",
                    "name": "The Sapphire Dome",
                    "type": "Fortress",
                    "faction": "Dinn",
                    "defender_strength": 100,
                    "under_siege": True,
                    "siege": {"besieger_faction": "Allakia", "matin_ticks_elapsed": 3},
                },
            ),
            _brief_cell(
                nearby_h3,
                stronghold={
                    "id": "sh_nearby",
                    "name": "Bemm",
                    "type": "City",
                    "faction": "Boonan",
                    "defender_strength": 50,
                },
            ),
        ],
    }

    sections = build_environs_brief(environs)

    assert sections.terrain.startswith(
        "The army is occupying the fortress of the Sapphire Dome, in open ground terrain."
    )
    assert "The stronghold is under siege by Allakia forces." in sections.terrain
    assert "matin" not in sections.terrain and "3" not in sections.terrain
    assert "Sapphire Dome" not in sections.strongholds
    assert "city of Bemm" in sections.strongholds


def test_environs_brief_describes_visible_nearby_siege_without_internal_state():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    nearby_h3 = sorted(h3.grid_ring(center_h3, 1))[0]
    environs = {
        "center_h3": center_h3,
        "radius": 1,
        "cells": [
            _brief_cell(center_h3),
            _brief_cell(
                nearby_h3,
                stronghold={
                    "id": "sh_9",
                    "name": "Bemm",
                    "type": "City",
                    "faction": "Boonan",
                    "defender_strength": 200,
                    "under_siege": True,
                    "siege": {
                        "besieger_faction": "Dinn",
                        "matin_ticks_elapsed": 4,
                        "gates_open": True,
                    },
                },
            ),
        ],
    }

    strongholds = build_environs_brief(environs).strongholds

    assert "controlled by Boonan, under siege by Dinn (garrison 200)" in strongholds
    assert "matin" not in strongholds
    assert "gates" not in strongholds


def test_environs_brief_opens_with_road_before_terrain():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    environs = {
        "center_h3": center_h3,
        "radius": 0,
        "cells": [_brief_cell(center_h3, terrain="Open Ground", road=True)],
    }

    sections = build_environs_brief(environs)

    assert sections.terrain == "The army is on the road in open ground terrain."


def test_environs_brief_distinguishes_visible_road_exit_and_dead_end():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    neighbors = sorted(h3.grid_ring(center_h3, 1))
    first_h3, second_h3 = next(
        (first, second)
        for first, second in combinations(neighbors, 2)
        if h3.grid_distance(first, second) == 2
    )
    border_h3 = next(
        cell_h3
        for cell_h3 in h3.grid_ring(first_h3, 1)
        if h3.grid_distance(center_h3, cell_h3) == 2
    )
    environs = {
        "center_h3": center_h3,
        "radius": 1,
        "cells": [
            _brief_cell(center_h3, road=True),
            _brief_cell(first_h3, road=True),
            _brief_cell(second_h3, road=True),
        ],
    }

    section = build_environs_brief(environs, border_road_cells=[border_h3]).roads

    assert "dead end" in section
    assert "towards" not in section
    assert section.count("The road leads") == 1


def test_environs_brief_collapses_road_loop_without_inventing_dead_end():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    ring = sorted(h3.grid_ring(center_h3, 1))
    environs = {
        "center_h3": center_h3,
        "radius": 1,
        "cells": [_brief_cell(center_h3, road=True)] + [_brief_cell(cell_h3, road=True) for cell_h3 in ring],
    }

    assert build_environs_brief(environs).roads == "The road loops through the area."


def test_environs_brief_uses_only_serialized_army_intelligence():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    close_h3 = sorted(h3.grid_ring(center_h3, 1))[0]
    medium_h3 = sorted(h3.grid_ring(center_h3, 3))[0]
    distant_h3 = sorted(h3.grid_ring(center_h3, 4))[0]
    close_bearing = location_description._bearing_word(center_h3, close_h3)
    medium_bearing = location_description._bearing_word(center_h3, medium_h3)
    distant_bearing = location_description._bearing_word(center_h3, distant_h3)
    stronghold = {
        "id": "sh_1",
        "name": "Bemm",
        "type": "city",
        "faction": "Boonan",
        "defender_strength": 0,
    }
    environs = {
        "center_h3": center_h3,
        "radius": 4,
        "cells": [
            _brief_cell(center_h3),
            _brief_cell(
                close_h3,
                stronghold=stronghold,
                armies=[
                    {
                        "army_id": "army_2",
                        "faction": "Delisgar",
                        "name": "The Blessed Banners",
                        "commander": "Baron Soman",
                        "infantry": 2500,
                        "cavalry": 500,
                    }
                ],
            ),
            _brief_cell(
                medium_h3,
                road=True,
                armies=[{"army_id": "army_3", "faction": "Dinn", "strength_rounded": 3000}],
            ),
            _brief_cell(
                distant_h3,
                terrain="Desert",
                armies=[{"army_id": "army_4", "faction": "Allakia"}],
            ),
        ],
    }

    sections = build_environs_brief(environs)

    assert sections.strongholds == (
        f"Nearby strongholds: city of Bemm 1 league to the {close_bearing}, "
        "controlled by Boonan (garrison 0)."
    )
    assert (
        'Delisgar army "The Blessed Banners" '
        "(strength 3,000, commanded by Baron Soman) occupying Bemm"
    ) in sections.armies
    occupying_phrase = sections.armies.split(";")[0]
    assert "league" not in occupying_phrase
    assert f"Dinn army (strength 3,000) on the road 3 leagues to the {medium_bearing}" in sections.armies
    assert f"Allakia army in desert terrain 4 leagues to the {distant_bearing}" in sections.armies
    assert "The Blessed Banners" in sections.armies
    assert "Baron Soman" in sections.armies
    assert "army_3" not in sections.armies and "army_4" not in sections.armies


def test_environs_brief_without_forageable_cells_is_exhausted():
    center_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    environs = {
        "center_h3": center_h3,
        "radius": 0,
        "cells": [_brief_cell(center_h3, terrain="Open Water", settlement=0, forage=0)],
    }

    assert build_environs_brief(environs).forage == "The area is exhausted in terms of forage."
