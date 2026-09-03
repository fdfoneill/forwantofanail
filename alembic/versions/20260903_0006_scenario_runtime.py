"""Bind a database to its external scenario package."""

from alembic import op
import sqlalchemy as sa

revision = "20260903_0006"
down_revision = "20260902_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "scenario_runtime" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "scenario_runtime",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("scenario_id", sa.String(100), nullable=False),
        sa.Column("scenario_version", sa.String(100), nullable=False),
        sa.Column("database_source_fingerprint", sa.String(64), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("singleton_id = 1", name="ck_scenario_runtime_singleton"),
    )


def downgrade() -> None:
    if "scenario_runtime" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("scenario_runtime")
