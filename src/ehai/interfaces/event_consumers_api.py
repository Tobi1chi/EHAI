"""HTTP transport and public schema definitions for durable external consumption."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ehai import JsonValue
from ehai.application.event_consumers import CONSUMER_ID_PATTERN, EventConsumerService
from ehai.interfaces.http_models import DataResponse, UuidInput
from ehai.interfaces.public_documents import public_json_value

ConsumerIdInput = Annotated[str, StringConstraints(pattern=CONSUMER_ID_PATTERN)]


class RegisterEventConsumerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consumer_id: ConsumerIdInput


class ReadEventConsumerBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=100, ge=1, le=1000, strict=True)


class AcknowledgeEventConsumerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_token: UuidInput


def build_event_consumers_router(service: EventConsumerService) -> APIRouter:
    """Build routes under the caller's /api/v1 router with its shared error handlers."""
    router = APIRouter(prefix="/event-consumers", tags=["event-consumers"])

    @router.post(
        "",
        response_model=DataResponse,
        operation_id="registerEventConsumer",
        responses=_responses("EventConsumer"),
    )
    def register_event_consumer(body: RegisterEventConsumerRequest) -> DataResponse:
        return DataResponse(data=public_json_value(service.register(body.consumer_id)))

    @router.get(
        "/{consumer_id}",
        response_model=DataResponse,
        operation_id="getEventConsumer",
        responses=_responses("EventConsumer"),
    )
    def get_event_consumer(consumer_id: ConsumerIdInput) -> DataResponse:
        return DataResponse(data=public_json_value(service.get(consumer_id)))

    @router.post(
        "/{consumer_id}/batches",
        operation_id="readConsumerEvents",
        response_model=DataResponse,
        responses=_responses("EventConsumerBatch"),
    )
    def read_event_consumer_batch(
        consumer_id: ConsumerIdInput, body: ReadEventConsumerBatchRequest
    ) -> DataResponse:
        return DataResponse(data=public_json_value(service.read(consumer_id, limit=body.limit)))

    @router.post(
        "/{consumer_id}/ack",
        operation_id="acknowledgeConsumerEvents",
        response_model=DataResponse,
        responses=_responses("EventConsumerAcknowledgement"),
    )
    def acknowledge_event_consumer(
        consumer_id: ConsumerIdInput, body: AcknowledgeEventConsumerRequest
    ) -> DataResponse:
        return DataResponse(
            data=public_json_value(service.acknowledge(consumer_id, batch_token=body.batch_token))
        )

    return router


def _responses(name: str) -> dict[int | str, dict[str, object]]:
    return {
        200: {
            "description": name,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"queries.schema.json#/$defs/{name}Response"}
                }
            },
        }
    }


def event_consumer_schema_definitions() -> dict[str, JsonValue]:
    """Definitions to merge into the canonical query schema before client generation."""
    consumer_id: JsonValue = {"type": "string", "pattern": CONSUMER_ID_PATTERN}
    offset: JsonValue = {"type": "integer", "minimum": 0}
    identifier: JsonValue = {"type": "string", "format": "uuid"}
    nullable_id: JsonValue = {"type": ["string", "null"], "format": "uuid"}
    definitions: dict[str, JsonValue] = {}
    properties_by_name: dict[str, dict[str, JsonValue]] = {
        "EventConsumer": {
            "consumer_id": consumer_id,
            "acknowledged_offset": offset,
            "acknowledged_event_id": nullable_id,
            "pending_batch_token": nullable_id,
            "created_at": {"type": "string", "format": "date-time"},
        },
        "EventConsumerBatch": {
            "consumer_id": consumer_id,
            "batch_token": nullable_id,
            "after_offset": offset,
            "through_offset": offset,
            "events": {
                "type": "array",
                "items": {"$ref": "events.schema.json#/$defs/StoredEventRecord"},
            },
        },
        "EventConsumerAcknowledgement": {
            "consumer_id": consumer_id,
            "batch_token": identifier,
            "acknowledged_offset": offset,
            "acknowledged_event_id": identifier,
        },
    }
    for name, properties in properties_by_name.items():
        definitions[name] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }
        definitions[f"{name}Response"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["data"],
            "properties": {"data": {"$ref": f"#/$defs/{name}"}},
        }
    return definitions
