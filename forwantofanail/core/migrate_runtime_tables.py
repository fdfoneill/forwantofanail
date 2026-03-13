from __future__ import annotations

from sqlalchemy import text

from forwantofanail.core.database import Base, create_session, get_engine
from forwantofanail.core.models import GameClock


def migrate_runtime_tables() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)

    session = create_session(engine)
    try:
        # Backfill lightweight runtime schema additions for existing SQLite files.
        table_info = session.execute(text("PRAGMA table_info(alerts)")).all()
        if table_info:
            columns = {row[1] for row in table_info}
            if "signal_kind" not in columns:
                session.execute(
                    text("ALTER TABLE alerts ADD COLUMN signal_kind VARCHAR(20) NOT NULL DEFAULT 'event'")
                )
                session.commit()

        if session.get(GameClock, 1) is None:
            session.add(GameClock(singleton_id=1, day=1, watch=1))
            session.commit()
    finally:
        session.close()


def main() -> None:
    migrate_runtime_tables()


if __name__ == "__main__":
    main()
