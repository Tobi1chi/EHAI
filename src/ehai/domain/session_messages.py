"""Persistent messages exchanged between existing Built-in Agent Sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ehai import ID, new_id, normalize_id, utc_now


class SessionMessageStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    READ = "read"


@dataclass(frozen=True, slots=True)
class SessionMessage:
    source_session_id: ID
    target_session_id: ID
    correlation_id: str
    content: str
    message_id: ID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    status: SessionMessageStatus = SessionMessageStatus.PENDING

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", normalize_id(self.message_id))
        object.__setattr__(self, "source_session_id", normalize_id(self.source_session_id))
        object.__setattr__(self, "target_session_id", normalize_id(self.target_session_id))
        if self.source_session_id == self.target_session_id:
            raise ValueError("Session Message source and target must differ")
        if not isinstance(self.correlation_id, str) or not self.correlation_id.strip():
            raise ValueError("Session Message correlation_id must not be blank")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Session Message content must not be blank")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("Session Message created_at must be timezone-aware")
        if not isinstance(self.status, SessionMessageStatus):
            raise TypeError("Session Message status must be SessionMessageStatus")

    def deliver(self) -> SessionMessage:
        if self.status is SessionMessageStatus.READ:
            return self
        return SessionMessage(
            self.source_session_id,
            self.target_session_id,
            self.correlation_id,
            self.content,
            self.message_id,
            self.created_at,
            SessionMessageStatus.DELIVERED,
        )

    def mark_read(self) -> SessionMessage:
        return SessionMessage(
            self.source_session_id,
            self.target_session_id,
            self.correlation_id,
            self.content,
            self.message_id,
            self.created_at,
            SessionMessageStatus.READ,
        )
