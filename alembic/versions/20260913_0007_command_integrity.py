"""Reset-required command integrity schema.

Fresh databases are created from current metadata by the baseline revision.
Reject an old physical schema rather than silently stamping it as compatible.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260913_0007"
down_revision = "20260903_0006"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    required = {
        "sieges": "live_besieger_army_id",
        "siege_participants": "live_besieger_army_id",
        "agent_runs": "lease_generation",
        "agent_run_sessions": "lease_generation",
    }
    for table, column in required.items():
        if column not in {row["name"] for row in inspector.get_columns(table)}:
            raise RuntimeError("This release requires a fresh game database. Recreate the database, run Alembic, then initialize the scenario.")
    if op.get_bind().dialect.name == "sqlite":
        ddl = op.get_bind().execute(sa.text("SELECT sql FROM sqlite_master WHERE name='armies'")).scalar_one()
        if "AUTOINCREMENT" not in ddl.upper():
            raise RuntimeError("Recreate this SQLite database for non-reusable army identities.")


def downgrade():
    raise RuntimeError("This reset-required release cannot downgrade an existing game. Recreate it with the older release.")
