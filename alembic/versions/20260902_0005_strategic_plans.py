"""Add structured agent plans and strategic-review state."""

from alembic import op
import sqlalchemy as sa


revision = "20260902_0005"
down_revision = "20260831_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    assignment_columns = {row["name"] for row in inspector.get_columns("agent_assignments")}
    additions = (
        ("consecutive_passive_watches", sa.Integer(), False, "0"),
        ("strategic_review_required", sa.Boolean(), False, sa.true()),
        ("strategic_review_reason", sa.String(120), True, None),
        ("plan_review_due_tick", sa.Integer(), True, None),
    )
    for name, kind, nullable, default in additions:
        if name not in assignment_columns:
            op.add_column("agent_assignments", sa.Column(name, kind, nullable=nullable, server_default=default))
    memory_columns = {row["name"] for row in sa.inspect(op.get_bind()).get_columns("agent_memory_revisions")}
    if "strategic_plan_json" not in memory_columns:
        op.add_column("agent_memory_revisions", sa.Column("strategic_plan_json", sa.Text(), nullable=True))
    op.execute(
        "UPDATE agent_assignments SET consecutive_passive_watches = 0, strategic_review_required = TRUE, "
        "strategic_review_reason = 'plan_required', plan_review_due_tick = NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("agent_memory_revisions") as batch:
        batch.drop_column("strategic_plan_json")
    with op.batch_alter_table("agent_assignments") as batch:
        batch.drop_column("plan_review_due_tick")
        batch.drop_column("strategic_review_reason")
        batch.drop_column("strategic_review_required")
        batch.drop_column("consecutive_passive_watches")
