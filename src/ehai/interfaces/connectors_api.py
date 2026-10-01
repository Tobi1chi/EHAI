"""Operator APIs and the separate token-authenticated connector wire protocol."""

from typing import Annotated

from fastapi import APIRouter, Header

from ehai import normalize_id
from ehai.application.connector_models import (
    ClaimConnectorCallRequest,
    ConnectorCall,
    ConnectorClaim,
    ConnectorConnection,
    ConnectorEventReceipt,
    ConnectorModel,
    InvokeConnectorRequest,
    ReconcileConnectorCallRequest,
    RegisterConnectorRequest,
    ReportConnectorResultRequest,
    SubmitConnectorEventRequest,
)
from ehai.application.connectors import ConnectorService
from ehai.interfaces.http_models import UuidInput

ConnectorToken = Annotated[str, Header(alias="X-EHAI-Connector-Token")]


class ConnectorResponse[T](ConnectorModel):
    data: T


def build_connectors_router(service: ConnectorService) -> APIRouter:
    router = APIRouter()

    @router.post("/projects/{project_id}/connectors", operation_id="registerConnector")
    def register(
        project_id: UuidInput, body: RegisterConnectorRequest
    ) -> ConnectorResponse[ConnectorConnection]:
        return ConnectorResponse(data=service.register(normalize_id(project_id), body))

    @router.get("/projects/{project_id}/connectors", operation_id="listConnectors")
    def connections(project_id: UuidInput) -> ConnectorResponse[list[ConnectorConnection]]:
        return ConnectorResponse(data=service.list_connections(normalize_id(project_id)))

    @router.get("/connectors/{connector_id}", operation_id="getConnector")
    def connection(connector_id: UuidInput) -> ConnectorResponse[ConnectorConnection]:
        return ConnectorResponse(data=service.get(normalize_id(connector_id)))

    @router.post("/connectors/{connector_id}/calls", operation_id="invokeConnector")
    def invoke(
        connector_id: UuidInput, body: InvokeConnectorRequest
    ) -> ConnectorResponse[ConnectorCall]:
        return ConnectorResponse(data=service.invoke(normalize_id(connector_id), body))

    @router.get("/connectors/{connector_id}/calls", operation_id="listConnectorCalls")
    def calls(connector_id: UuidInput) -> ConnectorResponse[list[ConnectorCall]]:
        return ConnectorResponse(data=service.list_calls(normalize_id(connector_id)))

    @router.get("/connector-calls/{call_id}", operation_id="getConnectorCall")
    def call(call_id: UuidInput) -> ConnectorResponse[ConnectorCall]:
        return ConnectorResponse(data=service.get_call(normalize_id(call_id)))

    @router.post("/connector-calls/{call_id}/reconcile", operation_id="reconcileConnectorCall")
    def reconcile(
        call_id: UuidInput, body: ReconcileConnectorCallRequest
    ) -> ConnectorResponse[ConnectorCall]:
        """Ask the original worker to verify an unknown result, without replaying a write."""
        return ConnectorResponse(data=service.reconcile(normalize_id(call_id), body))

    @router.post("/connector-bridge/{connector_id}/claims", operation_id="claimConnectorCall")
    def claim(
        connector_id: UuidInput, body: ClaimConnectorCallRequest, token: ConnectorToken = ""
    ) -> ConnectorResponse[ConnectorClaim | None]:
        return ConnectorResponse(data=service.claim(normalize_id(connector_id), token, body))

    @router.post("/connector-bridge/{connector_id}/results", operation_id="reportConnectorResult")
    def report(
        connector_id: UuidInput, body: ReportConnectorResultRequest, token: ConnectorToken = ""
    ) -> ConnectorResponse[ConnectorCall]:
        return ConnectorResponse(data=service.report(normalize_id(connector_id), token, body))

    @router.post("/connector-bridge/{connector_id}/events", operation_id="submitConnectorEvent")
    def event(
        connector_id: UuidInput, body: SubmitConnectorEventRequest, token: ConnectorToken = ""
    ) -> ConnectorResponse[ConnectorEventReceipt]:
        return ConnectorResponse(data=service.receive(normalize_id(connector_id), token, body))

    return router
