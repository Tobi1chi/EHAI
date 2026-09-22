"""Durable event delivery for external callers, without executing their decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ehai import ID, normalize_id
from ehai.application.ports import StoredEvent

CONSUMER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


class EventConsumerNotFoundError(LookupError):
    """The named consumer has not been registered on this source host."""


@dataclass(frozen=True, slots=True)
class EventConsumer:
    consumer_id: str
    acknowledged_offset: int
    acknowledged_event_id: ID | None
    pending_batch_token: ID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EventConsumerBatch:
    consumer_id: str
    batch_token: ID | None
    after_offset: int
    through_offset: int
    events: tuple[StoredEvent, ...]


@dataclass(frozen=True, slots=True)
class EventConsumerAcknowledgement:
    consumer_id: str
    batch_token: ID
    acknowledged_offset: int
    acknowledged_event_id: ID


class EventConsumerRepository(Protocol):
    """Atomic delivery operations; one outstanding batch per consumer/source."""

    def register(self, consumer_id: str) -> EventConsumer: ...

    def get(self, consumer_id: str) -> EventConsumer: ...

    def read(self, consumer_id: str, *, limit: int) -> EventConsumerBatch: ...

    def acknowledge(self, consumer_id: str, *, batch_token: ID) -> EventConsumerAcknowledgement: ...


class EventConsumerService:
    """Validate external consumption requests independently from execution control."""

    def __init__(self, repository: EventConsumerRepository) -> None:
        self._repository = repository

    def register(self, consumer_id: str) -> EventConsumer:
        """Register from the beginning, or return the existing durable position."""
        return self._repository.register(_consumer_id(consumer_id))

    def get(self, consumer_id: str) -> EventConsumer:
        """Read progress without delivering or acknowledging events."""
        return self._repository.get(_consumer_id(consumer_id))

    def read(self, consumer_id: str, *, limit: int = 100) -> EventConsumerBatch:
        """Deliver or replay one pending batch; an empty read has no token."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Event consumer batch limit must be between 1 and 1000")
        return self._repository.read(_consumer_id(consumer_id), limit=limit)

    def acknowledge(self, consumer_id: str, *, batch_token: str) -> EventConsumerAcknowledgement:
        """Acknowledge the entire delivered batch; never run a downstream action."""
        return self._repository.acknowledge(
            _consumer_id(consumer_id), batch_token=normalize_id(batch_token)
        )


def _consumer_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(CONSUMER_ID_PATTERN, value) is None:
        raise ValueError(
            "consumer_id must be 1-128 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value
