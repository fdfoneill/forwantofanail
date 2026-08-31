from __future__ import annotations

from itertools import combinations

import h3
import pytest

from forwantofanail.core.database import create_session, reset_database_runtime
from forwantofanail.core.migrate_runtime_tables import migrate_runtime_tables
from forwantofanail.core.models import Army, Detachment, Location, Stronghold, TerrainType
from forwantofanail.mechanics.navigation import RouteNotFoundError, build_route_summary


@pytest.fixture()
def navigation_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'navigation.db'}")
    monkeypatch.setenv("ADMIN_TOKEN", "navigation-test-admin")
    reset_database_runtime()
    migrate_runtime_tables()
    session = create_session()
    session.add_all(
        [
            TerrainType(
                terrain_id=1,
                terrain_name="Open Ground",
                speed_multiplier=1.0,
                scout_multiplier=1.0,
                is_water=False,
            ),
            TerrainType(
                terrain_id=2,
                terrain_name="Forest",
                speed_multiplier=0.5,
                scout_multiplier=0.5,
                is_water=False,
            ),
        ]
    )
    session.flush()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        reset_database_runtime()


def _line(length: int = 7) -> list[str]:
    center = h3.latlng_to_cell(41.0, 29.0, 7)
    ring = sorted(h3.grid_ring(center, (length - 1) // 2))
    for first, last in combinations(ring, 2):
        path = list(h3.grid_path_cells(first, last))
        if len(path) == length and center in path:
            return path
    raise AssertionError("Unable to construct synthetic H3 line")


def _location(session, cell_h3: str, *, road: bool, terrain_id: int = 1) -> None:
    session.add(
        Location(
            location_id=cell_h3,
            terrain_id=terrain_id,
            is_road=road,
            settlement=0,
            foraged_this_season=0,
        )
    )


def _stronghold(session, stronghold_id: int, name: str, cell_h3: str) -> Stronghold:
    row = Stronghold(
        stronghold_id=stronghold_id,
        stronghold_name=name,
        stronghold_type="fortress",
        location_id=cell_h3,
        control="Allakia",
        stronghold_threshold=10,
    )
    session.add(row)
    return row


def _army(session, cell_h3: str, *, wagons: int = 0) -> Army:
    row = Army(
        army_id=1,
        army_name="The Host",
        army_faction="Allakia",
        location_id=cell_h3,
        army_supply=100,
        army_morale=9,
        army_resting_morale=9,
        is_embarked=False,
        is_garrison=False,
    )
    session.add(row)
    session.flush()
    session.add(
        Detachment(
            detachment_id=1,
            detachment_name="Main Body",
            army_id=row.army_id,
            is_heavy=False,
            is_cavalry=False,
            wagon_count=wagons,
            warrior_count=1000,
            is_mercenary=False,
        )
    )
    session.flush()
    return row


def test_road_route_is_deterministic_and_split_at_named_landmark(navigation_session):
    path = _line()
    for index, cell_h3 in enumerate(path):
        _location(navigation_session, cell_h3, road=index not in {0, len(path) - 1})
    origin = _stronghold(navigation_session, 1, "Rushkegal", path[0])
    _stronghold(navigation_session, 2, "Highhold", path[3])
    destination = _stronghold(navigation_session, 3, "Leyke", path[-1])
    army = _army(navigation_session, path[0])
    navigation_session.flush()

    result = build_route_summary(
        navigation_session,
        army=army,
        origin=origin,
        destination=destination,
        allow_off_road=False,
    )

    assert result["route_type"] == "road_only"
    assert result["origin_stronghold_id"] == "sh_1"
    assert result["destination_stronghold_id"] == "sh_3"
    assert result["total_leagues"] == 6
    assert result["estimated_watches"] == 6
    assert [(leg["from"], leg["to"], leg["travel"]) for leg in result["legs"]] == [
        ("Rushkegal", "Highhold", "road"),
        ("Highhold", "Leyke", "road"),
    ]
    assert result["legs"][0]["from_stronghold_id"] == "sh_1"
    assert result["legs"][0]["to_stronghold_id"] == "sh_2"
    assert result["summary"] == (
        "From Rushkegal, travel 3 leagues by road to Highhold, then 3 leagues by road to Leyke."
    )
    assert result["initial_direction"] in {
        "north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"
    }
    assert "road toward Highhold" in result["initial_instruction"]
    assert not any(cell_h3 in repr(result) for cell_h3 in path)


def test_fastest_route_uses_static_terrain_costs(navigation_session):
    path = _line(5)
    for index, cell_h3 in enumerate(path):
        _location(
            navigation_session,
            cell_h3,
            road=False,
            terrain_id=2 if index == 2 else 1,
        )
    destination = _stronghold(navigation_session, 2, "Leyke", path[-1])
    army = _army(navigation_session, path[0])
    navigation_session.flush()

    result = build_route_summary(
        navigation_session,
        army=army,
        origin=None,
        destination=destination,
        allow_off_road=True,
    )

    assert result["route_type"] == "fastest"
    assert result["total_leagues"] == 4
    # Open ground costs two watches, forest four, and entry into the stronghold one.
    assert result["estimated_watches"] == 9
    assert result["legs"][0]["travel"] == "off_road"
    assert result["legs"][0]["terrains"] == ["Open Ground", "Forest"]
    assert path[0] not in repr(result)


def test_road_only_rejects_off_road_start(navigation_session):
    path = _line(5)
    for index, cell_h3 in enumerate(path):
        _location(navigation_session, cell_h3, road=index not in {0, len(path) - 1})
    destination = _stronghold(navigation_session, 2, "Leyke", path[-1])
    army = _army(navigation_session, path[0])
    navigation_session.flush()

    with pytest.raises(RouteNotFoundError, match="No route"):
        build_route_summary(
            navigation_session,
            army=army,
            origin=None,
            destination=destination,
            allow_off_road=False,
        )


def test_wagons_cannot_use_off_road_route(navigation_session):
    path = _line(5)
    for cell_h3 in path:
        _location(navigation_session, cell_h3, road=False)
    destination = _stronghold(navigation_session, 2, "Leyke", path[-1])
    army = _army(navigation_session, path[0], wagons=1)
    navigation_session.flush()

    with pytest.raises(RouteNotFoundError, match="No route"):
        build_route_summary(
            navigation_session,
            army=army,
            origin=None,
            destination=destination,
            allow_off_road=True,
        )


def test_route_to_current_stronghold_has_no_initial_cell_instruction(navigation_session):
    cell_h3 = h3.latlng_to_cell(41.0, 29.0, 7)
    _location(navigation_session, cell_h3, road=False)
    destination = _stronghold(navigation_session, 1, "Highhold", cell_h3)
    army = _army(navigation_session, cell_h3)
    navigation_session.flush()

    result = build_route_summary(
        navigation_session,
        army=army,
        origin=None,
        destination=destination,
        allow_off_road=False,
    )

    assert result["summary"] == "You are already at Highhold."
    assert result["legs"] == []
    assert result["initial_direction"] is None
    assert result["initial_instruction"] is None
