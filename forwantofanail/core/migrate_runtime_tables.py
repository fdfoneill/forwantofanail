from __future__ import annotations

from sqlalchemy import text

from forwantofanail.core.database import Base, create_session, get_engine
from forwantofanail.core.models import GameClock


def _has_duplicate_lower_values(session, table_name: str, column_name: str) -> bool:
    duplicate = session.execute(
        text(
            f"""
            SELECT lower({column_name}) AS normalized
            FROM {table_name}
            GROUP BY lower({column_name})
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    return duplicate is not None


def _create_sqlite_indexes(session) -> None:
    session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_actions_one_in_progress_per_commander
            ON actions (commander_id)
            WHERE state = 'in_progress'
            """
        )
    )
    session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_sieges_one_active_per_stronghold
            ON sieges (stronghold_id)
            WHERE state = 'active'
            """
        )
    )
    session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_siege_participants_one_active_per_army
            ON siege_participants (besieger_army_id)
            WHERE state = 'active'
            """
        )
    )
    if not _has_duplicate_lower_values(session, "commanders", "commander_name"):
        session.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_commanders_name_lower
                ON commanders (lower(commander_name))
                """
            )
        )
    if not _has_duplicate_lower_values(session, "armies", "army_name"):
        session.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_armies_name_lower
                ON armies (lower(army_name))
                """
            )
        )
    session.commit()


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

        if engine.dialect.name == "sqlite":
            _create_sqlite_indexes(session)
    finally:
        session.close()


def main() -> None:
    migrate_runtime_tables()


if __name__ == "__main__":
    main()
