from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url

    default_path = Path(__file__).resolve().parents[1] / "forwantofanail.db"
    return f"sqlite:///{default_path}"


def _sqlite_connect_args() -> dict[str, object]:
    return {
        "check_same_thread": False,
        "timeout": 30.0,
    }


def _configure_sqlite(connection, _record) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


@lru_cache(maxsize=4)
def _cached_engine(database_url: str, echo: bool) -> Engine:
    kwargs: dict[str, object] = {"echo": echo, "future": True}
    if database_url.startswith("sqlite:"):
        kwargs["connect_args"] = _sqlite_connect_args()
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite:"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def get_engine(echo: bool = False):
    return _cached_engine(get_database_url(), echo)


@lru_cache(maxsize=4)
def _cached_sessionmaker(database_url: str, echo: bool):
    return sessionmaker(bind=get_engine(echo=echo), autoflush=False, autocommit=False, future=True)


def create_session(engine=None):
    if engine is not None:
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        return Session()
    Session = _cached_sessionmaker(get_database_url(), False)
    return Session()
