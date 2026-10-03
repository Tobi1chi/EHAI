"""Persistence boundary for an opt-in routing experiment."""

from contextlib import AbstractContextManager
from typing import Literal, Protocol

from ehai import JsonValue
from ehai.application.connector_models import ConnectorModel
from ehai.application.ports import CommandReceiptStore
from ehai.domain.events import Event

RoutingDocumentKind = Literal["lab", "recipe", "request", "replay"]


class RoutingTransaction(Protocol):
    def get(self, kind: RoutingDocumentKind, entity_id: str) -> dict[str, JsonValue]: ...
    def list(
        self, kind: RoutingDocumentKind, lab_id: str | None = None
    ) -> list[dict[str, JsonValue]]: ...
    def save(self, kind: RoutingDocumentKind, entity_id: str, document: ConnectorModel) -> None: ...
    @property
    def receipts(self) -> CommandReceiptStore: ...
    def emit(self, event: Event) -> None: ...


class RoutingStore(Protocol):
    def transaction(self, *, write: bool) -> AbstractContextManager[RoutingTransaction]: ...
