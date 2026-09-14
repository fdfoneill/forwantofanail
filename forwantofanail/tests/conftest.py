import os
import uuid

import h3
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from forwantofanail.core.database import Base, get_engine, create_session, reset_database_runtime
from forwantofanail.core.models import Army, Commander, Detachment, GameClock, Location, Stronghold, TerrainType
from forwantofanail.core.sequences import synchronize_sequences


def pytest_addoption(parser):
    parser.addoption("--require-postgresql", action="store_true", help="Fail if the PostgreSQL integration database is unavailable")


def pytest_sessionstart(session):
    if session.config.getoption("--require-postgresql") and not os.getenv("TEST_DATABASE_URL"):
        pytest.exit("TEST_DATABASE_URL is required for PostgreSQL integration tests", returncode=2)


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def integrity_db(request, tmp_path, monkeypatch):
    admin_engine = None
    schema = None
    if request.param == "postgresql":
        raw = os.getenv("TEST_DATABASE_URL")
        if not raw:
            pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL test database")
        url = make_url(raw)
        if url.get_backend_name() != "postgresql" or "test" not in (url.database or "").lower():
            pytest.fail("TEST_DATABASE_URL must identify a PostgreSQL database with 'test' in its name")
        admin_engine = create_engine(url)
        schema = "integrity_" + uuid.uuid4().hex
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    else:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'integrity.db'}")
    monkeypatch.setenv("SESSION_SECRET", "integrity-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "integrity-admin")
    monkeypatch.setenv("OLLAMA_AGENT_MODEL", "test-model")
    reset_database_runtime()
    try:
        Base.metadata.create_all(get_engine())
        center = h3.latlng_to_cell(40, -75, 8)
        cells = sorted(h3.grid_disk(center, 2))
        neighbor = sorted(h3.grid_ring(center, 1))[0]
        far = next(cell for cell in cells if h3.grid_distance(center, cell) == 2)
        with create_session() as session:
            session.add(TerrainType(terrain_id=1, terrain_name="Open Ground", speed_multiplier=1, scout_multiplier=1))
            session.add_all(Location(location_id=cell, terrain_id=1, is_road=True, settlement=2) for cell in cells)
            session.add_all(Commander(commander_id=i, commander_name=f"Commander {i}", commander_title="Captain", commander_age=30) for i in range(3))
            session.flush()
            session.add_all(Army(army_id=i + 1, commander_id=i, location_id=center if i < 2 else far,
                                 army_name=f"Army {i}", army_faction="Blue" if i < 2 else "Red",
                                 army_supply=100, army_morale=9, army_resting_morale=9) for i in range(3))
            session.flush()
            session.add_all(Detachment(detachment_id=i, army_id=i, detachment_name=f"Foot {i}", warrior_count=100) for i in range(1, 4))
            session.add(Detachment(detachment_id=4, army_id=1, detachment_name="Reserve", warrior_count=100))
            session.add(Stronghold(stronghold_id=1, location_id=far, stronghold_name="Fort", stronghold_type="town", control="Red", stronghold_threshold=0))
            session.add(GameClock(singleton_id=1, day=1, watch=1, world_tick=0))
            synchronize_sequences(session)
            session.commit()
        yield {"backend": request.param, "center": center, "neighbor": neighbor, "far": far}
    finally:
        reset_database_runtime()
        if admin_engine is not None:
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin_engine.dispose()
