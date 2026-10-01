"""A durable command/result and inbound-event boundary for external processes."""

import hashlib
import hmac
from contextlib import AbstractContextManager
from typing import Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from ehai import JsonValue, json_dumps, new_id, normalize_id, utc_now
from ehai.application.connector_models import (
    ClaimConnectorCallRequest,
    ConnectorCall,
    ConnectorClaim,
    ConnectorConnection,
    ConnectorEventReceipt,
    InvokeConnectorRequest,
    ReconcileConnectorCallRequest,
    RegisterConnectorRequest,
    ReportConnectorResultRequest,
    SubmitConnectorEventRequest,
)
from ehai.application.ports import StateConflictError
from ehai.application.queries import QueryNotFoundError
from ehai.domain.events import Event, EventType


class ConnectorAuthenticationError(ValueError):
    pass


class ConnectorTransaction(Protocol):
    def require_project(self, project_id: str) -> None: ...
    def connection(self, connector_id: str) -> tuple[ConnectorConnection, str]: ...
    def connections(self, project_id: str) -> list[ConnectorConnection]: ...
    def save_connection(self, connection: ConnectorConnection, digest: str) -> None: ...
    def call(self, call_id: str) -> tuple[ConnectorCall, str | None, str | None]: ...
    def calls(self, connector_id: str) -> list[ConnectorCall]: ...
    def save_call(self, call: ConnectorCall, owner: str | None, token: str | None) -> None: ...
    def receipt(self, scope: str, key: str, fingerprint: str) -> dict[str, JsonValue] | None: ...
    def remember(
        self, scope: str, key: str, fingerprint: str, result: dict[str, JsonValue]
    ) -> None: ...
    def emit(self, event: Event) -> None: ...


class ConnectorStore(Protocol):
    def transaction(self, *, write: bool) -> AbstractContextManager[ConnectorTransaction]: ...


def validate_connector_schema(schema: dict[str, JsonValue]) -> None:
    """Only local references; validation must never fetch a schema from a network URL."""

    def inspect(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, dict):
            if "$id" in value:
                raise ValueError("Connector schemas must not change their resolution base with $id")
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = value.get(key)
                if reference is not None and (
                    not isinstance(reference, str) or not reference.startswith("#")
                ):
                    raise ValueError("Connector schemas support only local references")
            if "uniqueItems" in value:
                raise ValueError("Connector schemas must omit uniqueItems")
            for item in value.values():
                inspect(item)

    inspect(schema)
    if schema.get("type") != "object":
        raise ValueError("Connector data schemas must describe objects")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError("Invalid connector JSON Schema") from error


def _validate(schema: dict[str, JsonValue], value: dict[str, JsonValue]) -> None:
    if len(json_dumps(value).encode()) > 1_000_000:
        raise ValueError("Connector data exceeds the 1 MB payload limit")
    try:
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
            value
        )
    except (ValidationError, Unresolvable) as error:
        raise ValueError("Connector payload does not match its registered schema") from error


class ConnectorService:
    def __init__(self, store: ConnectorStore) -> None:
        self.store = store

    def register(self, project_id: str, request: RegisterConnectorRequest) -> ConnectorConnection:
        for action in request.manifest.actions:
            validate_connector_schema(action.input_schema)
            validate_connector_schema(action.output_schema)
        for event in request.manifest.events:
            validate_connector_schema(event.data_schema)
        fingerprint = json_dumps({"project_id": project_id, **request.model_dump(mode="json")})
        with self.store.transaction(write=True) as tx:
            old = tx.receipt("register", request.idempotency_key, fingerprint)
            if old is not None:
                return ConnectorConnection.model_validate(old)
            tx.require_project(project_id)
            connection = ConnectorConnection(
                connector_id=str(new_id()),
                project_id=project_id,
                name=request.name,
                manifest=request.manifest,
                configuration=request.configuration,
                created_at=utc_now(),
            )
            tx.save_connection(connection, request.credential_sha256)
            tx.remember(
                "register", request.idempotency_key, fingerprint, connection.model_dump(mode="json")
            )
            return connection

    def get(self, connector_id: str) -> ConnectorConnection:
        with self.store.transaction(write=False) as tx:
            return tx.connection(connector_id)[0]

    def list_connections(self, project_id: str) -> list[ConnectorConnection]:
        with self.store.transaction(write=False) as tx:
            tx.require_project(project_id)
            return tx.connections(project_id)

    def list_calls(self, connector_id: str) -> list[ConnectorCall]:
        with self.store.transaction(write=False) as tx:
            tx.connection(connector_id)
            return tx.calls(connector_id)

    def get_call(self, call_id: str) -> ConnectorCall:
        with self.store.transaction(write=False) as tx:
            return tx.call(call_id)[0]

    def invoke(self, connector_id: str, request: InvokeConnectorRequest) -> ConnectorCall:
        fingerprint = json_dumps(request.model_dump(mode="json"))
        with self.store.transaction(write=True) as tx:
            old = tx.receipt("invoke:" + connector_id, request.idempotency_key, fingerprint)
            if old is not None:
                return ConnectorCall.model_validate(old)
            connection, _ = tx.connection(connector_id)
            action = next(
                (
                    a
                    for a in connection.manifest.actions
                    if a.name == request.action and a.version == request.action_version
                ),
                None,
            )
            if action is None:
                raise ValueError("Unknown connector action/version")
            if not action.read_only and not request.authorize_write:
                raise StateConflictError(
                    "This connector action requires explicit write authorization"
                )
            _validate(action.input_schema, request.inputs)
            now = utc_now()
            call = ConnectorCall(
                call_id=str(new_id()),
                connector_id=connector_id,
                project_id=connection.project_id,
                action=action,
                inputs=request.inputs,
                write_authorized=request.authorize_write,
                status="queued",
                output=None,
                error=None,
                external_operation_ref=None,
                created_at=now,
                updated_at=now,
            )
            tx.save_call(call, None, None)
            self._call_event(tx, call)
            tx.remember(
                "invoke:" + connector_id,
                request.idempotency_key,
                fingerprint,
                call.model_dump(mode="json"),
            )
            return call

    @staticmethod
    def _authorize(tx: ConnectorTransaction, connector_id: str, token: str) -> ConnectorConnection:
        try:
            connection, digest = tx.connection(connector_id)
        except QueryNotFoundError as error:
            raise ConnectorAuthenticationError("Invalid connector credentials") from error
        if not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), digest):
            raise ConnectorAuthenticationError("Invalid connector credentials")
        return connection

    def reconcile(self, call_id: str, request: ReconcileConnectorCallRequest) -> ConnectorCall:
        fingerprint = json_dumps(request.model_dump(mode="json"))
        with self.store.transaction(write=True) as tx:
            old = tx.receipt("reconcile:" + call_id, request.idempotency_key, fingerprint)
            if old is not None:
                return ConnectorCall.model_validate(old)
            call, owner, token = tx.call(call_id)
            if call.status != "unknown" or owner is None or token is None:
                raise StateConflictError("Only an unknown claimed call can be reconciled")
            call = call.model_copy(update={"status": "claimed", "updated_at": utc_now()})
            tx.save_call(call, owner, token)
            self._call_event(tx, call)
            tx.remember(
                "reconcile:" + call_id,
                request.idempotency_key,
                fingerprint,
                call.model_dump(mode="json"),
            )
            return call

    def claim(
        self, connector_id: str, token: str, request: ClaimConnectorCallRequest
    ) -> ConnectorClaim | None:
        with self.store.transaction(write=True) as tx:
            connection = self._authorize(tx, connector_id, token)
            calls = tx.calls(connector_id)
            for candidate in calls:
                if candidate.status != "claimed":
                    continue
                call, owner, claim_token = tx.call(candidate.call_id)
                if owner == request.worker_id and claim_token is not None:
                    return ConnectorClaim(
                        connection=connection, call=call, claim_token=claim_token, mode="reconcile"
                    )
            queued = next((c for c in calls if c.status == "queued"), None)
            if queued is None:
                return None
            claim_token = str(new_id())
            call = queued.model_copy(update={"status": "claimed", "updated_at": utc_now()})
            tx.save_call(call, request.worker_id, claim_token)
            self._call_event(tx, call)
            return ConnectorClaim(
                connection=connection, call=call, claim_token=claim_token, mode="execute"
            )

    def report(
        self, connector_id: str, token: str, request: ReportConnectorResultRequest
    ) -> ConnectorCall:
        fingerprint = json_dumps(request.model_dump(mode="json"))
        with self.store.transaction(write=True) as tx:
            self._authorize(tx, connector_id, token)
            old = tx.receipt("report:" + connector_id, request.delivery_id, fingerprint)
            if old is not None:
                return ConnectorCall.model_validate(old)
            call, owner, claim_token = tx.call(str(normalize_id(request.call_id)))
            if (
                call.connector_id != connector_id
                or claim_token is None
                or not hmac.compare_digest(claim_token, request.claim_token)
            ):
                raise StateConflictError("Result does not belong to this connector claim")
            if call.status not in {"claimed", "unknown"}:
                raise StateConflictError("Connector call already has a final result")
            if request.output is not None:
                _validate(call.action.output_schema, request.output)
            call = call.model_copy(
                update={
                    "status": request.status,
                    "output": request.output,
                    "error": request.error,
                    "external_operation_ref": request.external_operation_ref,
                    "updated_at": utc_now(),
                }
            )
            tx.save_call(call, owner, claim_token)
            self._call_event(tx, call)
            tx.remember(
                "report:" + connector_id,
                request.delivery_id,
                fingerprint,
                call.model_dump(mode="json"),
            )
            return call

    def receive(
        self, connector_id: str, token: str, request: SubmitConnectorEventRequest
    ) -> ConnectorEventReceipt:
        fingerprint = json_dumps(request.model_dump(mode="json"))
        with self.store.transaction(write=True) as tx:
            connection = self._authorize(tx, connector_id, token)
            old = tx.receipt("event:" + connector_id, request.event_id, fingerprint)
            if old is not None:
                return ConnectorEventReceipt.model_validate(old)
            contract = next(
                (
                    e
                    for e in connection.manifest.events
                    if e.name == request.event_type and e.version == request.schema_version
                ),
                None,
            )
            if contract is None:
                raise ValueError("Unregistered connector event type/version")
            _validate(contract.data_schema, request.data)
            event = Event(
                type=EventType.CONNECTOR_EVENT_RECEIVED,
                correlation_id=connector_id,
                payload={
                    "connector_id": connector_id,
                    "project_id": connection.project_id,
                    "external_event": request.model_dump(mode="json"),
                },
            )
            tx.emit(event)
            receipt = ConnectorEventReceipt(
                connector_id=connector_id,
                project_id=connection.project_id,
                event_id=request.event_id,
                core_event_id=str(event.id),
                received_at=event.occurred_at,
            )
            tx.remember(
                "event:" + connector_id,
                request.event_id,
                fingerprint,
                receipt.model_dump(mode="json"),
            )
            return receipt

    @staticmethod
    def _call_event(tx: ConnectorTransaction, call: ConnectorCall) -> None:
        tx.emit(
            Event(
                type=EventType.CONNECTOR_CALL_CHANGED,
                correlation_id=call.call_id,
                payload=call.model_dump(mode="json"),
            )
        )
