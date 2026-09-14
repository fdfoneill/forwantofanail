"""Keep PostgreSQL generated identities ahead of explicit scenario imports."""
from sqlalchemy import Integer, func, select, text
from .database import Base


def synchronize_sequences(session):
    if session.bind.dialect.name != "postgresql":
        return
    session.flush()
    for table in Base.metadata.sorted_tables:
        keys = list(table.primary_key.columns)
        if len(keys) != 1 or not isinstance(keys[0].type, Integer):
            continue
        column = keys[0]
        sequence = session.execute(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                   {"table": table.name, "column": column.name}).scalar()
        if not sequence:
            continue
        maximum = session.execute(select(func.max(column))).scalar()
        if maximum is None:
            continue
        # regclass resolves only the server-returned sequence identity. Quoting
        # both qualified parts avoids interpreting identifiers as SQL text.
        quoted = ".".join(session.bind.dialect.identifier_preparer.quote(part.strip('"'))
                          for part in sequence.split('.'))
        last, called = session.execute(text(f"SELECT last_value, is_called FROM {quoted}")).one()
        if maximum > last or (maximum == last and not called):
            session.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
                            {"sequence": sequence, "value": maximum})
