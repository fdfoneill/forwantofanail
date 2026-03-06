from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
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

    location_id = Column(String(15), primary_key=True)
    is_road = Column(Boolean, nullable=False, default=False)
    region = Column(String(100), nullable=True)
    terrain_id = Column(Integer, ForeignKey("terrain_types.terrain_id"), nullable=False)
    settlement = Column(Integer, nullable=False, default=0)
    foraged_this_season = Column(Boolean, nullable=False, default=False)

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


class CommanderTrait(Base):
    __tablename__ = "commander_traits"
    __table_args__ = (PrimaryKeyConstraint("commander_id", "trait_name"),)

    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False)
    trait_name = Column(String(100), nullable=False)

    commander = relationship("Commander", back_populates="traits")


class Army(Base):
    __tablename__ = "armies"

    army_id = Column(Integer, primary_key=True)
    location_id = Column(String(15), ForeignKey("locations.location_id"), nullable=False)
    army_name = Column(String(100), nullable=False)
    army_faction = Column(String(100), nullable=False)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True)
    army_supply = Column(Integer, nullable=False, default=0)
    army_morale = Column(Integer, nullable=False, default=0)
    is_embarked = Column(Boolean, nullable=False, default=False)
    is_garrison = Column(Boolean, nullable=False, default=False)
    noncombattant_count = Column(Integer, nullable=False, default=0)

    location = relationship("Location", back_populates="armies")
    commander = relationship("Commander", back_populates="armies")
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

    singleton_id = Column(Integer, primary_key=True, default=1)
    day = Column(Integer, nullable=False, default=1)
    watch = Column(Integer, nullable=False, default=1)


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    token = Column(String(128), primary_key=True)
    commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)

    commander = relationship("Commander", back_populates="auth_tokens")


class Action(Base):
    __tablename__ = "actions"

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


class Message(Base):
    __tablename__ = "messages"

    message_id = Column(Integer, primary_key=True, autoincrement=True)
    sender_commander_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=True, index=True)
    sender_stronghold_id = Column(Integer, ForeignKey("strongholds.stronghold_id"), nullable=True, index=True)
    sender_name = Column(String(100), nullable=False)
    recipient_id = Column(Integer, ForeignKey("commanders.commander_id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    priority = Column(String(20), nullable=False, default="normal")
    sent_day = Column(Integer, nullable=False)
    sent_watch = Column(Integer, nullable=False)
    delivery_day = Column(Integer, nullable=False)
    delivery_watch = Column(Integer, nullable=False)
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
