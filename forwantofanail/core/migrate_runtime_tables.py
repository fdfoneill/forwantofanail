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
    session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_event_key ON alerts (event_key) WHERE event_key IS NOT NULL"))
    session.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_recipient_status_delivery ON messages (recipient_id, status, delivery_tick)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS ix_alert_recipients_feed ON alert_recipients (commander_id, available_tick, alert_id)"))
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
        # Compatibility only for existing local SQLite databases. Production schema
        # changes are applied by Alembic before application startup.
        if engine.dialect.name == "sqlite":
            additions = {
                "alerts": {
                    "signal_kind": "VARCHAR(20) NOT NULL DEFAULT 'event'",
                    "created_tick": "INTEGER NOT NULL DEFAULT 0",
                    "available_tick": "INTEGER NOT NULL DEFAULT 0",
                    "event_key": "VARCHAR(160)",
                },
                "messages": {
                    "sent_tick": "INTEGER NOT NULL DEFAULT 0",
                    "delivery_tick": "INTEGER NOT NULL DEFAULT 0",
                },
                "game_clock": {"world_tick": "INTEGER NOT NULL DEFAULT 0"},
                "auth_tokens": {
                    "last_used_at": "DATETIME",
                    "revoked_at": "DATETIME",
                    "client_kind": "VARCHAR(20) NOT NULL DEFAULT 'api'",
                },
                "commanders": {
                    "created_by_commander_id": "INTEGER",
                    "created_day": "INTEGER",
                    "created_watch": "INTEGER",
                },
            }
            for table_name, table_additions in additions.items():
                table_info = session.execute(text(f"PRAGMA table_info({table_name})")).all()
                columns = {row[1] for row in table_info}
                for column_name, definition in table_additions.items():
                    if column_name not in columns:
                        session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))
            session.execute(text("UPDATE auth_tokens SET last_used_at = created_at WHERE last_used_at IS NULL"))
            watch_rank = "CASE watch WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3 WHEN 0 THEN 4 END"
            session.execute(text(f"UPDATE game_clock SET world_tick = ((day - 1) * 5) + {watch_rank}"))
            sent_rank = "CASE sent_watch WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3 WHEN 0 THEN 4 END"
            delivery_rank = "CASE delivery_watch WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3 WHEN 0 THEN 4 END"
            session.execute(text(f"UPDATE messages SET sent_tick = ((sent_day - 1) * 5) + {sent_rank}"))
            session.execute(text(f"UPDATE messages SET delivery_tick = ((delivery_day - 1) * 5) + {delivery_rank}"))
            created_rank = "CASE created_watch WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3 WHEN 0 THEN 4 END"
            available_rank = "CASE delivered_watch WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3 WHEN 0 THEN 4 END"
            session.execute(text(f"UPDATE alerts SET created_tick = ((created_day - 1) * 5) + {created_rank}"))
            session.execute(text(f"UPDATE alerts SET available_tick = ((delivered_day - 1) * 5) + {available_rank}"))
            session.execute(
                text(
                    """
                    UPDATE locations
                    SET foraged_this_season = CASE
                        WHEN CAST(foraged_this_season AS INTEGER) < 0 THEN 0
                        WHEN CAST(foraged_this_season AS INTEGER) > 3 THEN 3
                        ELSE CAST(foraged_this_season AS INTEGER)
                    END
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO alert_recipients (alert_id, commander_id, available_tick)
                    SELECT alert_id, recipient_commander_id, available_tick
                    FROM alerts WHERE recipient_commander_id IS NOT NULL
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO alert_recipients (alert_id, commander_id, available_tick)
                    SELECT alerts.alert_id, commanders.commander_id, alerts.available_tick
                    FROM alerts CROSS JOIN commanders
                    WHERE alerts.recipient_commander_id IS NULL
                    """
                )
            )
            session.commit()

        if session.get(GameClock, 1) is None:
            session.add(GameClock(singleton_id=1, day=1, watch=1, world_tick=0))
            session.commit()

        if engine.dialect.name == "sqlite":
            _create_sqlite_indexes(session)
    finally:
        session.close()


def main() -> None:
    migrate_runtime_tables()


if __name__ == "__main__":
    main()
