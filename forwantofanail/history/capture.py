from __future__ import annotations

import argparse

from sqlalchemy import text

from forwantofanail.core.database import create_session
from forwantofanail.core.models import GameClock
from forwantofanail.history.snapshots import capture_world_snapshot


WORLD_ADVISORY_LOCK_ID = 1180298062


def capture_current(*, finalize: bool = False) -> int:
    session = create_session()
    try:
        with session.begin():
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text(f"SELECT pg_advisory_xact_lock({WORLD_ADVISORY_LOCK_ID})"))
            clock = session.query(GameClock).filter(GameClock.singleton_id == 1).with_for_update().one()
            capture_world_snapshot(session, clock, is_final=finalize)
            return int(clock.world_tick)
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture the current authoritative world-history snapshot.")
    parser.add_argument("--finalize", action="store_true", help="Mark the current watch final for postgame export.")
    args = parser.parse_args()
    tick = capture_current(finalize=args.finalize)
    state = "final" if args.finalize else "provisional"
    print(f"Captured {state} world snapshot at tick {tick}.")


if __name__ == "__main__":
    main()
