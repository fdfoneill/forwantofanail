"""Transaction entry and lock order shared by API commands and workers."""


def begin_write(session):
    # SQLAlchemy can have an autobegun transaction while sqlite3 has not yet
    # started one. Reserve the writer before reading any mutation baseline.
    if session.bind.dialect.name == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if not session.new and not session.dirty and not session.deleted:
        session.expire_all()


def lock_clock(session, *, exclusive=False):
    from .models import GameClock
    query = session.query(GameClock).filter(GameClock.singleton_id == 1)
    if session.bind.dialect.name == "postgresql":
        query = query.with_for_update(read=not exclusive)
    return query.one_or_none()


def lock_commanders(session, commander_ids):
    from .models import Commander
    ids = sorted(set(value for value in commander_ids if value is not None))
    if session.bind.dialect.name == "postgresql" and ids:
        session.query(Commander.commander_id).filter(Commander.commander_id.in_(ids)).order_by(
            Commander.commander_id
        ).with_for_update().all()


def lock_armies(session, army_ids):
    from .models import Army, Detachment, Action
    ids = sorted(set(army_ids))
    if session.bind.dialect.name != "postgresql" or not ids:
        return
    rows = session.query(Army.army_id, Army.commander_id).filter(Army.army_id.in_(ids)).order_by(
        Army.army_id
    ).with_for_update().all()
    session.query(Detachment.detachment_id).filter(Detachment.army_id.in_(ids)).order_by(
        Detachment.detachment_id
    ).with_for_update().all()
    commanders = [row.commander_id for row in rows if row.commander_id is not None]
    session.query(Action.action_id).filter(Action.commander_id.in_(commanders)).order_by(
        Action.action_id
    ).with_for_update().all()
