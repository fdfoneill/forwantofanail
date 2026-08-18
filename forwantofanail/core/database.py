from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


_ENGINE = None
_ENGINE_URL = None
_SESSION_FACTORY = None
_SESSION_FACTORY_ENGINE = None


def get_database_url() -> str:
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url

    default_path = Path(__file__).resolve().parents[1] / "forwantofanail.db"
    return f"sqlite:///{default_path}"


def get_engine(echo: bool = False):
    global _ENGINE, _ENGINE_URL

    database_url = get_database_url()
    if _ENGINE is not None and _ENGINE_URL == database_url:
        return _ENGINE

    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {
            "check_same_thread": False,
            "timeout": float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30")),
        }

    engine = create_engine(database_url, echo=echo, future=True, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    _ENGINE = engine
    _ENGINE_URL = database_url
    return engine


def create_session(engine=None):
    global _SESSION_FACTORY, _SESSION_FACTORY_ENGINE

    if engine is None:
        engine = get_engine()

    if _SESSION_FACTORY is None or _SESSION_FACTORY_ENGINE is not engine:
        _SESSION_FACTORY = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
        _SESSION_FACTORY_ENGINE = engine
    return _SESSION_FACTORY()


def reset_database_runtime() -> None:
    global _ENGINE, _ENGINE_URL, _SESSION_FACTORY, _SESSION_FACTORY_ENGINE

    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _ENGINE_URL = None
    _SESSION_FACTORY = None
    _SESSION_FACTORY_ENGINE = None
