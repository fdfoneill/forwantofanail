from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from forwantofanail.core.database import Base, create_session, get_engine
from forwantofanail.core.models import Commander, CommanderRuntime, GameClock, StandingOrder


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

        commander_ids = [int(row[0]) for row in session.query(Commander.commander_id).all()]
        if commander_ids:
            existing_standing = {
                int(row[0])
                for row in session.query(StandingOrder.commander_id)
                .filter(StandingOrder.commander_id.in_(commander_ids))
                .all()
            }
            existing_runtime = {
                int(row[0])
                for row in session.query(CommanderRuntime.commander_id)
                .filter(CommanderRuntime.commander_id.in_(commander_ids))
                .all()
            }
            now = datetime.now(timezone.utc)
            default_scratchpad = json.dumps(
                {
                    "current_hypotheses": [],
                    "pending_correspondence": [],
                    "standing_intent": "",
                    "deferred_checks": [],
                    "notes": [],
                }
            )
            for commander_id in commander_ids:
                if commander_id not in existing_standing:
                    session.add(
                        StandingOrder(
                            commander_id=commander_id,
                            follow_road_enabled=False,
                            forced_march_enabled=False,
                            last_report=None,
                            last_report_day=None,
                            last_report_watch=None,
                            updated_at=now,
                        )
                    )
                if commander_id not in existing_runtime:
                    session.add(
                        CommanderRuntime(
                            commander_id=commander_id,
                            controller_type="human",
                            ai_enabled=False,
                            attention_needed=False,
                            attention_reasons_json="[]",
                            scratchpad_json=default_scratchpad,
                        )
                    )
            session.commit()
    finally:
        session.close()


def main() -> None:
    migrate_runtime_tables()


if __name__ == "__main__":
    main()
