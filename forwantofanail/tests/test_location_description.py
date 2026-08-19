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
from forwantofanail.mechanics.location_description import describe_army_location


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
