from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LoginRequest(BaseModel):
    commander_name: str


class ActionCreateRequest(BaseModel):
    kind: Literal["move"]
    destination_h3: str


class MessageCreateRequest(BaseModel):
    recipient_id: str
    content: str
    priority: str = "normal"


class TimePayload(BaseModel):
    day: int
    watch: int
    watch_label: str
