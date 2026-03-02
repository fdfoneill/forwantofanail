from __future__ import annotations

from forwantofanail.core.database import Base, create_session, get_engine
from forwantofanail.core.models import GameClock


def migrate_runtime_tables() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)

    session = create_session(engine)
    try:
        if session.get(GameClock, 1) is None:
            session.add(GameClock(singleton_id=1, day=1, watch=1))
            session.commit()
    finally:
        session.close()


def main() -> None:
    migrate_runtime_tables()


if __name__ == "__main__":
    main()
