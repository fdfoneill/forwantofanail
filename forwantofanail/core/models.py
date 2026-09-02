from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from .database import Base


class TerrainType(Base):
    __tablename__ = "terrain_types"

    terrain_id = Column(Integer, primary_key=True)
    terrain_name = Column(String(100), nullable=False)
    speed_multiplier = Column(Float, nullable=False, default=1.0)
    scout_multiplier = Column(Float, nullable=False, default=1.0)
    is_water = Column(Boolean, nullable=False, default=False)

    locations = relationship("Location", back_populates="terrain_type")


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        CheckConstraint(
            "foraged_this_season >= 0 AND foraged_this_season <= 3",
            name="ck_locations_forage_depletion_range",
        ),
    )

    location_id = Column(String(15), primary_key=True)
    is_road = Column(Boolean, nullable=False, default=False)
    region = Column(String(100), nullable=True)
    terrain_id = Column(Integer, ForeignKey("terrain_types.terrain_id"), nullable=False)
    settlement = Column(Integer, nullable=False, default=0)
    foraged_this_season = Column(Integer, nullable=False, default=0, server_default="0")

    terrain_type = relationship("TerrainType", back_populates="locations")
    armies = relationship("Army", back_populates="location")
    strongholds = relationship("Stronghold", back_populates="location")
    movements = relationship("Movement", back_populates="location")


class Commander(Base):
    __tablename__ = "commanders"

    commander_id = Column(Integer, primary_key=True)
    commander_name = Column(String(100), nullable=False)
    commander_age = Column(Integer, nullable=False)
    commander_title = Column(String(100), nullable=False)
    created_by_commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True)
    created_day = Column(Integer, nullable=True)
    created_watch = Column(Integer, nullable=True)

    traits = relationship("CommanderTrait", back_populates="commander", cascade="all, delete-orphan")
    armies = relationship("Army", back_populates="commander")
    actions = relationship("Action", back_populates="commander", cascade="all, delete-orphan")
    sent_messages = relationship(
        "Message",
        back_populates="sender_commander",
        cascade="all, delete-orphan",
        foreign_keys="Message.sender_commander_id",
    )
    received_messages = relationship(
        "Message",
        back_populates="recipient",
        cascade="all, delete-orphan",
        foreign_keys="Message.recipient_id",
    )
    auth_tokens = relationship("AuthToken", back_populates="commander", cascade="all, delete-orphan")
    claim = relationship(
        "CommanderClaim",
        back_populates="commander",
        uselist=False,
        cascade="all, delete-orphan",
    )
    alerts = relationship(
        "Alert",
        back_populates="recipient",
        cascade="all, delete-orphan",
        foreign_keys="Alert.recipient_commander_id",
    )
    standing_order = relationship(
        "StandingOrder",
        back_populates="commander",
        uselist=False,
        cascade="all, delete-orphan",
    )
    agent_assignment = relationship(
        "AgentAssignment", back_populates="commander", uselist=False, cascade="all, delete-orphan"
    )
    agent_dossier = relationship(
        "AgentCommanderDossier", back_populates="commander", uselist=False, cascade="all, delete-orphan"
    )


class CommanderTrait(Base):
    __tablename__ = "commander_traits"
    __table_args__ = (PrimaryKeyConstraint("commander_id", "trait_name"),)

    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False)
    trait_name = Column(String(100), nullable=False)

    commander = relationship("Commander", back_populates="traits")


class Army(Base):
    __tablename__ = "armies"
    __table_args__ = (
        CheckConstraint("army_morale >= 2 AND army_morale <= 12", name="ck_armies_morale_range"),
        CheckConstraint(
            "army_resting_morale >= 2 AND army_resting_morale <= 12",
            name="ck_armies_resting_morale_range",
        ),
        CheckConstraint("noncombattant_percent >= 0", name="ck_armies_noncombattant_percent_nonnegative"),
        CheckConstraint("army_supply >= 0", name="ck_armies_supply_nonnegative"),
    )

    army_id = Column(Integer, primary_key=True)
    location_id = Column(String(15), ForeignKey("locations.location_id"), nullable=False)
    army_name = Column(String(100), nullable=False)
    army_faction = Column(String(100), nullable=False)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True)
    garrison_stronghold_id = Column(Integer, ForeignKey("strongholds.stronghold_id"), nullable=True, unique=True)
    army_supply = Column(Integer, nullable=False, default=0)
    army_morale = Column(Integer, nullable=False, default=9)
    army_resting_morale = Column(Integer, nullable=False, default=9)
    is_embarked = Column(Boolean, nullable=False, default=False)
    is_garrison = Column(Boolean, nullable=False, default=False)
    noncombattant_percent = Column(Float, nullable=False, default=0.25)

    location = relationship("Location", back_populates="armies")
    commander = relationship("Commander", back_populates="armies")
    garrison_stronghold = relationship("Stronghold", foreign_keys=[garrison_stronghold_id])
    detachments = relationship("Detachment", back_populates="army", cascade="all, delete-orphan")
    movements = relationship("Movement", back_populates="army", cascade="all, delete-orphan")


class Detachment(Base):
    __tablename__ = "detachments"

    detachment_id = Column(Integer, primary_key=True)
    detachment_name = Column(String(100), nullable=False)
    army_id = Column(Integer, ForeignKey("armies.army_id"), nullable=False)
    is_heavy = Column(Boolean, nullable=False, default=False)
    is_cavalry = Column(Boolean, nullable=False, default=False)
    wagon_count = Column(Integer, nullable=False, default=0)
    warrior_count = Column(Integer, nullable=False, default=0)
    is_mercenary = Column(Boolean, nullable=False, default=False)

    army = relationship("Army", back_populates="detachments")
    specials = relationship(
        "DetachmentSpecial", back_populates="detachment", cascade="all, delete-orphan"
    )


class DetachmentSpecial(Base):
    __tablename__ = "detachment_specials"
    __table_args__ = (PrimaryKeyConstraint("detachment_id", "special_name"),)

    detachment_id = Column(Integer, ForeignKey("detachments.detachment_id"), nullable=False)
    special_name = Column(String(100), nullable=False)

    detachment = relationship("Detachment", back_populates="specials")


class Stronghold(Base):
    __tablename__ = "strongholds"

    stronghold_id = Column(Integer, primary_key=True)
    location_id = Column(String(15), ForeignKey("locations.location_id"), nullable=False)
    stronghold_name = Column(String(100), nullable=False, unique=True)
    stronghold_type = Column(String(30), nullable=False)
    control = Column(String(30), nullable=False)
    stronghold_threshold = Column(Integer, nullable=False, default=0)

    location = relationship("Location", back_populates="strongholds")
    sent_messages = relationship(
        "Message",
        back_populates="sender_stronghold",
        foreign_keys="Message.sender_stronghold_id",
    )
    sieges = relationship("Siege", back_populates="stronghold", cascade="all, delete-orphan")


class Siege(Base):
    __tablename__ = "sieges"

    siege_id = Column(Integer, primary_key=True, autoincrement=True)
    stronghold_id = Column(Integer, ForeignKey("strongholds.stronghold_id"), nullable=False, index=True)
    besieger_army_id = Column(Integer, ForeignKey("armies.army_id"), nullable=False, index=True)
    besieger_commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True, index=True)
    started_day = Column(Integer, nullable=False)
    started_watch = Column(Integer, nullable=False)
    matin_ticks_elapsed = Column(Integer, nullable=False, default=0)
    current_resistance = Column(Float, nullable=False)
    max_resistance = Column(Float, nullable=False)
    gates_open = Column(Boolean, nullable=False, default=False)
    state = Column(String(20), nullable=False, default="active", index=True)
    ended_day = Column(Integer, nullable=True)
    ended_watch = Column(Integer, nullable=True)
    ended_reason = Column(String(40), nullable=True)

    stronghold = relationship("Stronghold", back_populates="sieges")
    besieger_army = relationship("Army", foreign_keys=[besieger_army_id])
    besieger_commander = relationship("Commander", foreign_keys=[besieger_commander_id])
    participants = relationship("SiegeParticipant", back_populates="siege", cascade="all, delete-orphan")


class SiegeParticipant(Base):
    __tablename__ = "siege_participants"
    __table_args__ = (
        PrimaryKeyConstraint("siege_id", "besieger_army_id"),
    )

    siege_id = Column(Integer, ForeignKey("sieges.siege_id"), nullable=False)
    besieger_army_id = Column(Integer, ForeignKey("armies.army_id"), nullable=False, index=True)
    besieger_commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True, index=True)
    started_day = Column(Integer, nullable=False)
    started_watch = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False, default="active", index=True)
    ended_day = Column(Integer, nullable=True)
    ended_watch = Column(Integer, nullable=True)
    ended_reason = Column(String(40), nullable=True)

    siege = relationship("Siege", back_populates="participants")
    besieger_army = relationship("Army", foreign_keys=[besieger_army_id])
    besieger_commander = relationship("Commander", foreign_keys=[besieger_commander_id])


class Movement(Base):
    __tablename__ = "movements"
    __table_args__ = (
        PrimaryKeyConstraint("army_id", "date", "watch", "location_id"),
    )

    army_id = Column(Integer, ForeignKey("armies.army_id"), nullable=False)
    location_id = Column(String(15), ForeignKey("locations.location_id"), nullable=False)
    date = Column(Date, nullable=False)
    watch = Column(Integer, nullable=False)

    army = relationship("Army", back_populates="movements")
    location = relationship("Location", back_populates="movements")


class GameClock(Base):
    __tablename__ = "game_clock"
    __table_args__ = (
        CheckConstraint("day >= 1", name="ck_game_clock_day"),
        CheckConstraint("watch >= 0 AND watch <= 4", name="ck_game_clock_watch"),
        CheckConstraint("world_tick >= 0", name="ck_game_clock_tick"),
    )

    singleton_id = Column(Integer, primary_key=True, default=1)
    day = Column(Integer, nullable=False, default=1)
    watch = Column(Integer, nullable=False, default=1)
    world_tick = Column(Integer, nullable=False, default=0)


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    __table_args__ = (CheckConstraint("client_kind IN ('browser', 'api')", name="ck_auth_tokens_client_kind"),)

    token = Column(String(128), primary_key=True)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    client_kind = Column(String(20), nullable=False, default="api")

    commander = relationship("Commander", back_populates="auth_tokens")
    claim = relationship("CommanderClaim", back_populates="auth_token", uselist=False)


class CommanderClaim(Base):
    __tablename__ = "commander_claims"
    __table_args__ = (
        UniqueConstraint("token", name="uq_commander_claims_token"),
    )

    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), primary_key=True)
    token = Column(String(128), ForeignKey("auth_tokens.token"), nullable=False)
    claimed_at = Column(DateTime, nullable=False)

    commander = relationship("Commander", back_populates="claim")
    auth_token = relationship("AuthToken", back_populates="claim")


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint("kind IN ('move', 'forage', 'attack', 'besiege', 'rout')", name="ck_actions_kind"),
        CheckConstraint("state IN ('queued', 'in_progress', 'completed', 'cancelled', 'failed')", name="ck_actions_state"),
    )

    action_id = Column(Integer, primary_key=True, autoincrement=True)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False, index=True)
    kind = Column(String(40), nullable=False)
    state = Column(String(30), nullable=False, default="queued", index=True)
    parameters_json = Column(Text, nullable=False, default="{}")
    accepted_at = Column(DateTime, nullable=False)
    started_day = Column(Integer, nullable=True)
    started_watch = Column(Integer, nullable=True)
    eta_day = Column(Integer, nullable=True)
    eta_watch = Column(Integer, nullable=True)

    commander = relationship("Commander", back_populates="actions")


class StandingOrder(Base):
    __tablename__ = "standing_orders"

    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), primary_key=True)
    follow_road_enabled = Column(Boolean, nullable=False, default=False)
    forced_march_enabled = Column(Boolean, nullable=False, default=False)
    last_report = Column(Text, nullable=True)
    last_report_day = Column(Integer, nullable=True)
    last_report_watch = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False)

    commander = relationship("Commander", back_populates="standing_order")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("status IN ('in_transit', 'received', 'lost')", name="ck_messages_status"),
        Index("ix_messages_recipient_status_delivery", "recipient_id", "status", "delivery_tick"),
    )

    message_id = Column(Integer, primary_key=True, autoincrement=True)
    sender_commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True, index=True)
    sender_stronghold_id = Column(Integer, ForeignKey("strongholds.stronghold_id"), nullable=True, index=True)
    sender_name = Column(String(100), nullable=False)
    recipient_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    priority = Column(String(20), nullable=False, default="normal")
    sent_day = Column(Integer, nullable=False)
    sent_watch = Column(Integer, nullable=False)
    sent_tick = Column(Integer, nullable=False, default=0)
    delivery_day = Column(Integer, nullable=False)
    delivery_watch = Column(Integer, nullable=False)
    delivery_tick = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="in_transit", index=True)
    is_read = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, nullable=False)

    sender_commander = relationship(
        "Commander",
        back_populates="sent_messages",
        foreign_keys=[sender_commander_id],
    )
    sender_stronghold = relationship(
        "Stronghold",
        back_populates="sent_messages",
        foreign_keys=[sender_stronghold_id],
    )
    recipient = relationship("Commander", back_populates="received_messages", foreign_keys=[recipient_id])


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint("alert_type IN ('world event', 'action', 'report', 'violence', 'morale')", name="ck_alerts_type"),
        CheckConstraint("signal_kind IN ('event', 'state')", name="ck_alerts_signal_kind"),
        CheckConstraint("importance IN ('low', 'normal', 'moderate', 'high')", name="ck_alerts_importance"),
        CheckConstraint("created_tick >= 0 AND available_tick >= 0", name="ck_alerts_ticks"),
    )

    alert_id = Column(Integer, primary_key=True, autoincrement=True)
    recipient_commander_id = Column(
        Integer,
        ForeignKey("commanders.commander_id"),
        nullable=True,
        index=True,
    )
    alert_type = Column(String(20), nullable=False, index=True)
    signal_kind = Column(String(20), nullable=False, default="event", index=True)
    category = Column(String(40), nullable=False, default="general", index=True)
    importance = Column(String(20), nullable=False, default="normal", index=True)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    created_day = Column(Integer, nullable=False, index=True)
    created_watch = Column(Integer, nullable=False, index=True)
    created_tick = Column(Integer, nullable=False, default=0, index=True)
    delivered_day = Column(Integer, nullable=False, index=True)
    delivered_watch = Column(Integer, nullable=False, index=True)
    available_tick = Column(Integer, nullable=False, default=0, index=True)
    is_read = Column(Boolean, nullable=False, default=False, index=True)
    event_key = Column(String(160), nullable=True, unique=True)
    created_at = Column(DateTime, nullable=False, index=True)

    recipient = relationship(
        "Commander",
        back_populates="alerts",
        foreign_keys=[recipient_commander_id],
    )
    recipients = relationship("AlertRecipient", back_populates="alert", cascade="all, delete-orphan")


class AlertRecipient(Base):
    __tablename__ = "alert_recipients"
    __table_args__ = (
        PrimaryKeyConstraint("alert_id", "commander_id"),
        Index("ix_alert_recipients_feed", "commander_id", "available_tick", "alert_id"),
        Index("ix_alert_recipients_unread", "commander_id", "read_at"),
    )

    alert_id = Column(Integer, ForeignKey("alerts.alert_id", ondelete="CASCADE"), nullable=False)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id", ondelete="CASCADE"), nullable=False)
    available_tick = Column(Integer, nullable=False)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)

    alert = relationship("Alert", back_populates="recipients")
    commander = relationship("Commander")


class WorldSnapshot(Base):
    __tablename__ = "world_snapshots"
    __table_args__ = (
        CheckConstraint("world_tick >= 0", name="ck_world_snapshots_tick"),
        CheckConstraint("day >= 1", name="ck_world_snapshots_day"),
        CheckConstraint("watch >= 0 AND watch <= 4", name="ck_world_snapshots_watch"),
        CheckConstraint("schema_version >= 1", name="ck_world_snapshots_schema_version"),
    )

    world_tick = Column(Integer, primary_key=True)
    day = Column(Integer, nullable=False)
    watch = Column(Integer, nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    state_json = Column(Text, nullable=False)
    is_final = Column(Boolean, nullable=False, default=False, index=True)
    captured_at = Column(DateTime, nullable=False, index=True)


class WorldHistoryEvent(Base):
    __tablename__ = "world_history_events"
    __table_args__ = (
        CheckConstraint("world_tick >= 0", name="ck_world_history_events_tick"),
        CheckConstraint(
            "event_kind IN ('battle', 'stronghold_conquest', 'siege_started', 'siege_ended', "
            "'army_created', 'army_destroyed')",
            name="ck_world_history_events_kind",
        ),
        Index("ix_world_history_events_tick_kind", "world_tick", "event_kind"),
    )

    event_id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(240), nullable=False, unique=True)
    world_tick = Column(Integer, nullable=False, index=True)
    event_kind = Column(String(40), nullable=False, index=True)
    location_id = Column(String(15), ForeignKey("locations.location_id"), nullable=True, index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, index=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("actor_scope", "route", "idempotency_key", name="uq_idempotency_scope_route_key"),
    )

    record_id = Column(Integer, primary_key=True, autoincrement=True)
    actor_scope = Column(String(160), nullable=False, index=True)
    route = Column(String(160), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    response_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, index=True)


class AgentCommanderDossier(Base):
    __tablename__ = "agent_commander_dossiers"

    commander_id = Column(Integer, ForeignKey("commanders.commander_id", ondelete="CASCADE"), primary_key=True)
    source_kind = Column(String(20), nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    content_json = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    commander = relationship("Commander", back_populates="agent_dossier")


class AgentAssignment(Base):
    __tablename__ = "agent_assignments"

    commander_id = Column(Integer, ForeignKey("commanders.commander_id", ondelete="CASCADE"), primary_key=True)
    profile_id = Column(String(80), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    current_memory_revision = Column(Integer, nullable=False, default=0)
    consecutive_passive_watches = Column(Integer, nullable=False, default=0)
    strategic_review_required = Column(Boolean, nullable=False, default=True)
    strategic_review_reason = Column(String(120), nullable=True)
    plan_review_due_tick = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    commander = relationship("Commander", back_populates="agent_assignment")
    runs = relationship("AgentRun", back_populates="assignment", cascade="all, delete-orphan")
    memories = relationship("AgentMemoryRevision", back_populates="assignment", cascade="all, delete-orphan")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'timed_out', 'skipped', 'obsolete')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint("trigger IN ('watch', 'assignment', 'retry', 'reconcile')", name="ck_agent_runs_trigger"),
        CheckConstraint("world_tick >= 0", name="ck_agent_runs_world_tick"),
        CheckConstraint("attempt >= 1", name="ck_agent_runs_attempt"),
        UniqueConstraint("commander_id", "world_tick", "attempt", name="uq_agent_runs_commander_tick_attempt"),
        Index("ix_agent_runs_queue", "status", "created_at", "run_id"),
    )

    run_id = Column(Integer, primary_key=True, autoincrement=True)
    commander_id = Column(Integer, ForeignKey("agent_assignments.commander_id", ondelete="CASCADE"), nullable=False, index=True)
    world_tick = Column(Integer, nullable=False, index=True)
    attempt = Column(Integer, nullable=False, default=1)
    trigger = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="queued", index=True)
    profile_id = Column(String(80), nullable=False)
    provider = Column(String(30), nullable=True)
    model = Column(String(160), nullable=True)
    rules_hash = Column(String(64), nullable=True)
    dossier_hash = Column(String(64), nullable=True)
    starting_memory_revision = Column(Integer, nullable=False, default=0)
    ending_memory_revision = Column(Integer, nullable=True)
    lease_owner = Column(String(120), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False)
    model_turns = Column(Integer, nullable=False, default=0)
    tool_calls = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    final_summary_json = Column(Text, nullable=True)
    error_code = Column(String(80), nullable=True)
    error_message = Column(Text, nullable=True)

    assignment = relationship("AgentAssignment", back_populates="runs")
    events = relationship("AgentRunEvent", back_populates="run", cascade="all, delete-orphan")
    sessions = relationship("AgentRunSession", back_populates="run", cascade="all, delete-orphan")


class AgentRunEvent(Base):
    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_sequence"),
        Index("ix_agent_run_events_run_sequence", "run_id", "sequence"),
    )

    event_id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    event_kind = Column(String(40), nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False)
    duration_ms = Column(Integer, nullable=True)

    run = relationship("AgentRun", back_populates="events")


class AgentMemoryRevision(Base):
    __tablename__ = "agent_memory_revisions"
    __table_args__ = (
        PrimaryKeyConstraint("commander_id", "revision"),
    )

    commander_id = Column(Integer, ForeignKey("agent_assignments.commander_id", ondelete="CASCADE"), nullable=False)
    revision = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    strategic_plan_json = Column(Text, nullable=True)
    author_kind = Column(String(20), nullable=False)
    run_id = Column(Integer, ForeignKey("agent_runs.run_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False)

    assignment = relationship("AgentAssignment", back_populates="memories")


class AgentRunSession(Base):
    __tablename__ = "agent_run_sessions"

    token_hash = Column(String(64), primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)

    run = relationship("AgentRun", back_populates="sessions")


class AgentWorkerHeartbeat(Base):
    __tablename__ = "agent_worker_heartbeats"

    worker_id = Column(String(120), primary_key=True)
    concurrency = Column(Integer, nullable=False, default=1)
    started_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False, index=True)
    runtime_version = Column(String(30), nullable=False, default="1")


# Cross-process invariants. Partial unique indexes are supported by both
# PostgreSQL (production) and SQLite (development/tests).
Index(
    "uq_actions_one_in_progress_per_commander",
    Action.commander_id,
    unique=True,
    postgresql_where=Action.state == "in_progress",
    sqlite_where=Action.state == "in_progress",
)
Index(
    "uq_sieges_one_active_per_stronghold",
    Siege.stronghold_id,
    unique=True,
    postgresql_where=Siege.state == "active",
    sqlite_where=Siege.state == "active",
)
Index(
    "uq_siege_participants_one_active_per_army",
    SiegeParticipant.besieger_army_id,
    unique=True,
    postgresql_where=SiegeParticipant.state == "active",
    sqlite_where=SiegeParticipant.state == "active",
)
Index(
    "uq_armies_one_per_commander",
    Army.commander_id,
    unique=True,
    postgresql_where=Army.commander_id.is_not(None),
    sqlite_where=Army.commander_id.is_not(None),
)
Index("uq_commanders_name_lower", func.lower(Commander.commander_name), unique=True)
Index("uq_armies_name_lower", func.lower(Army.army_name), unique=True)
