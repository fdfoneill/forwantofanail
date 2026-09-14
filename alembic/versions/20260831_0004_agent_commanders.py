"""Add persistent LLM commander assignments, heartbeats, memory, and diagnostics."""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0004"
down_revision = "20260819_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The historical baseline revision builds current metadata on a brand-new
    # database. In that case these tables already exist by the time this
    # revision is visited; established databases still need the DDL below.
    if sa.inspect(op.get_bind()).has_table("agent_commander_dossiers"):
        return
    op.create_table(
        "agent_commander_dossiers",
        sa.Column("commander_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["commander_id"], ["commanders.commander_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("commander_id"),
    )
    op.create_table(
        "agent_assignments",
        sa.Column("commander_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_memory_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["commander_id"], ["commanders.commander_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("commander_id"),
    )
    op.create_index("ix_agent_assignments_enabled", "agent_assignments", ["enabled"])
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commander_id", sa.Integer(), nullable=False),
        sa.Column("world_tick", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("profile_id", sa.String(80), nullable=False),
        sa.Column("provider", sa.String(30), nullable=True),
        sa.Column("model", sa.String(160), nullable=True),
        sa.Column("rules_hash", sa.String(64), nullable=True),
        sa.Column("dossier_hash", sa.String(64), nullable=True),
        sa.Column("starting_memory_revision", sa.Integer(), nullable=False),
        sa.Column("ending_memory_revision", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("model_turns", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("final_summary_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'timed_out', 'skipped', 'obsolete')", name="ck_agent_runs_status"),
        sa.CheckConstraint("trigger IN ('watch', 'assignment', 'retry', 'reconcile')", name="ck_agent_runs_trigger"),
        sa.CheckConstraint("world_tick >= 0", name="ck_agent_runs_world_tick"),
        sa.CheckConstraint("attempt >= 1", name="ck_agent_runs_attempt"),
        sa.ForeignKeyConstraint(["commander_id"], ["agent_assignments.commander_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("commander_id", "world_tick", "attempt", name="uq_agent_runs_commander_tick_attempt"),
    )
    op.create_index("ix_agent_runs_commander_id", "agent_runs", ["commander_id"])
    op.create_index("ix_agent_runs_world_tick", "agent_runs", ["world_tick"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_lease_expires_at", "agent_runs", ["lease_expires_at"])
    op.create_index("ix_agent_runs_queue", "agent_runs", ["status", "created_at", "run_id"])
    op.create_table(
        "agent_run_events",
        sa.Column("event_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_kind", sa.String(40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_sequence"),
    )
    op.create_index("ix_agent_run_events_run_sequence", "agent_run_events", ["run_id", "sequence"])
    op.create_table(
        "agent_memory_revisions",
        sa.Column("commander_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_kind", sa.String(20), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["commander_id"], ["agent_assignments.commander_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("commander_id", "revision"),
    )
    op.create_table(
        "agent_run_sessions",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("commander_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["commander_id"], ["commanders.commander_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_agent_run_sessions_run_id", "agent_run_sessions", ["run_id"])
    op.create_index("ix_agent_run_sessions_commander_id", "agent_run_sessions", ["commander_id"])
    op.create_index("ix_agent_run_sessions_expires_at", "agent_run_sessions", ["expires_at"])
    op.create_table(
        "agent_worker_heartbeats",
        sa.Column("worker_id", sa.String(120), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("runtime_version", sa.String(30), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index("ix_agent_worker_heartbeats_last_seen_at", "agent_worker_heartbeats", ["last_seen_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table_name in (
        "agent_worker_heartbeats", "agent_run_sessions", "agent_memory_revisions",
        "agent_run_events", "agent_runs", "agent_assignments", "agent_commander_dossiers",
    ):
        if inspector.has_table(table_name):
            op.drop_table(table_name)
            inspector = sa.inspect(op.get_bind())
