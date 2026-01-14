from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url

    default_path = Path(__file__).resolve().parents[1] / "forwantofanail.db"
    return f"sqlite:///{default_path}"


def get_engine(echo: bool = False):
    return create_engine(get_database_url(), echo=echo, future=True)


def create_session(engine=None):
    if engine is None:
        engine = get_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return Session()
