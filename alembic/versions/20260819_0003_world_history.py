"""Add authoritative per-watch world snapshots and structured history events."""

from alembic import op
import sqlalchemy as sa


revision = "20260819_0003"
down_revision = "20260818_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("world_snapshots"):
        op.create_table(
            "world_snapshots",
            sa.Column("world_tick", sa.Integer(), nullable=False),
            sa.Column("day", sa.Integer(), nullable=False),
            sa.Column("watch", sa.Integer(), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False),
            sa.Column("state_json", sa.Text(), nullable=False),
            sa.Column("is_final", sa.Boolean(), nullable=False),
            sa.Column("captured_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("world_tick >= 0", name="ck_world_snapshots_tick"),
            sa.CheckConstraint("day >= 1", name="ck_world_snapshots_day"),
            sa.CheckConstraint("watch >= 0 AND watch <= 4", name="ck_world_snapshots_watch"),
            sa.CheckConstraint("schema_version >= 1", name="ck_world_snapshots_schema_version"),
            sa.PrimaryKeyConstraint("world_tick"),
        )
        op.create_index("ix_world_snapshots_is_final", "world_snapshots", ["is_final"])
        op.create_index("ix_world_snapshots_captured_at", "world_snapshots", ["captured_at"])

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("world_history_events"):
        op.create_table(
            "world_history_events",
            sa.Column("event_id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_key", sa.String(length=240), nullable=False),
            sa.Column("world_tick", sa.Integer(), nullable=False),
            sa.Column("event_kind", sa.String(length=40), nullable=False),
            sa.Column("location_id", sa.String(length=15), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("world_tick >= 0", name="ck_world_history_events_tick"),
            sa.CheckConstraint(
                "event_kind IN ('battle', 'stronghold_conquest', 'siege_started', 'siege_ended', "
                "'army_created', 'army_destroyed')",
                name="ck_world_history_events_kind",
            ),
            sa.ForeignKeyConstraint(["location_id"], ["locations.location_id"]),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint("event_key"),
        )
        op.create_index("ix_world_history_events_world_tick", "world_history_events", ["world_tick"])
        op.create_index("ix_world_history_events_event_kind", "world_history_events", ["event_kind"])
        op.create_index("ix_world_history_events_location_id", "world_history_events", ["location_id"])
        op.create_index("ix_world_history_events_created_at", "world_history_events", ["created_at"])
        op.create_index(
            "ix_world_history_events_tick_kind",
            "world_history_events",
            ["world_tick", "event_kind"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("world_history_events"):
        op.drop_table("world_history_events")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("world_snapshots"):
        op.drop_table("world_snapshots")
