"""Version 1 of the process-independent Connector HTTP contract."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ehai import JsonValue

ConnectorText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
ConnectorKey = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")]


class ConnectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectorAction(ConnectorModel):
    name: ConnectorKey
    version: ConnectorText
    description: ConnectorText
    read_only: bool = Field(strict=True)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


class ConnectorEventType(ConnectorModel):
    name: ConnectorKey
    version: ConnectorText
    data_schema: dict[str, JsonValue]


class ConnectorManifest(ConnectorModel):
    protocol_version: Literal[1] = 1
    connector_type: ConnectorKey
    version: ConnectorText
    actions: list[ConnectorAction] = Field(min_length=1, max_length=50)
    events: list[ConnectorEventType] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_contracts(self) -> ConnectorManifest:
        for values in (self.actions, self.events):
            names = [(v.name, v.version) for v in values]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate connector contract")
        return self


class RegisterConnectorRequest(ConnectorModel):
    idempotency_key: ConnectorText
    name: ConnectorText
    manifest: ConnectorManifest
    configuration: dict[str, JsonValue]
    credential_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ConnectorConnection(ConnectorModel):
    connector_id: str
    project_id: str
    name: str
    manifest: ConnectorManifest
    configuration: dict[str, JsonValue]
    created_at: AwareDatetime


class InvokeConnectorRequest(ConnectorModel):
    idempotency_key: ConnectorText
    action: ConnectorKey
    action_version: ConnectorText
    inputs: dict[str, JsonValue]
    authorize_write: bool = Field(default=False, strict=True)


class ConnectorCall(ConnectorModel):
    call_id: str
    connector_id: str
    project_id: str
    action: ConnectorAction
    inputs: dict[str, JsonValue]
    write_authorized: bool
    status: Literal["queued", "claimed", "completed", "failed", "unknown"]
    output: dict[str, JsonValue] | None
    error: str | None
    external_operation_ref: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ClaimConnectorCallRequest(ConnectorModel):
    protocol_version: Literal[1] = 1
    worker_id: ConnectorKey


class ReconcileConnectorCallRequest(ConnectorModel):
    idempotency_key: ConnectorText


class ConnectorClaim(ConnectorModel):
    protocol_version: Literal[1] = 1
    connection: ConnectorConnection
    call: ConnectorCall
    claim_token: str
    mode: Literal["execute", "reconcile"]


class ReportConnectorResultRequest(ConnectorModel):
    protocol_version: Literal[1] = 1
    delivery_id: ConnectorText
    call_id: str
    claim_token: str
    status: Literal["completed", "failed", "unknown"]
    output: dict[str, JsonValue] | None = None
    error: Annotated[str, StringConstraints(max_length=2000)] | None = None
    external_operation_ref: Annotated[str, StringConstraints(max_length=2000)] | None = None

    @model_validator(mode="after")
    def result_shape(self) -> ReportConnectorResultRequest:
        if self.status == "completed" and (self.output is None or self.error is not None):
            raise ValueError("Completed results require output and no error")
        if self.status != "completed" and (self.output is not None or not self.error):
            raise ValueError("Failed/unknown results require an error and no output")
        return self


class SubmitConnectorEventRequest(ConnectorModel):
    protocol_version: Literal[1] = 1
    event_id: ConnectorText
    event_type: ConnectorKey
    schema_version: ConnectorText
    occurred_at: AwareDatetime
    subject: ConnectorText
    data: dict[str, JsonValue]


class ConnectorEventReceipt(ConnectorModel):
    connector_id: str
    project_id: str
    event_id: str
    core_event_id: str
    received_at: AwareDatetime
