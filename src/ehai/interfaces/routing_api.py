"""Normal HTTP operations for explicitly created dual-feedback routing labs."""

from fastapi import APIRouter

from ehai import normalize_id
from ehai.application.connector_models import ConnectorModel
from ehai.application.routing_lab import RoutingLabService
from ehai.application.routing_models import (
    ConfigureRoutingFallbackRequest,
    ConfigureRoutingProjectRequest,
    CreateRoutingLabRequest,
    PauseRoutingRecipeRequest,
    ProposeRoutingRecipeRequest,
    PublishRoutingRecipeRequest,
    ResolveRoutingRequest,
    RoutingFeedbackRequest,
    RoutingLabMetrics,
    RoutingLabView,
    RoutingRecipe,
    RoutingReplay,
    RoutingRequest,
    StartRoutingReplayRequest,
    SubmitRoutingRequest,
)
from ehai.interfaces.http_models import UuidInput


class RoutingResponse[T](ConnectorModel):
    data: T


def build_routing_router(service: RoutingLabService) -> APIRouter:
    router = APIRouter()

    @router.post("/routing-labs/{lab_id}/change-project", operation_id="configureRoutingProject")
    def configure_project(
        lab_id: UuidInput, body: ConfigureRoutingProjectRequest
    ) -> RoutingResponse[RoutingLabView]:
        return RoutingResponse(data=service.configure_project(normalize_id(lab_id), body))

    @router.post("/routing-labs/{lab_id}/fallback", operation_id="configureRoutingFallback")
    def configure_fallback(
        lab_id: UuidInput, body: ConfigureRoutingFallbackRequest
    ) -> RoutingResponse[RoutingLabView]:
        return RoutingResponse(data=service.configure_fallback(normalize_id(lab_id), body))

    @router.post("/projects/{project_id}/routing-labs", operation_id="createRoutingLab")
    def create(
        project_id: UuidInput, body: CreateRoutingLabRequest
    ) -> RoutingResponse[RoutingLabView]:
        return RoutingResponse(data=service.create(normalize_id(project_id), body))

    @router.get("/routing-labs/{lab_id}", operation_id="getRoutingLab")
    def lab(lab_id: UuidInput) -> RoutingResponse[RoutingLabView]:
        return RoutingResponse(data=service.lab(normalize_id(lab_id)))

    @router.post("/routing-labs/{lab_id}/requests", operation_id="submitRoutingRequest")
    def submit(lab_id: UuidInput, body: SubmitRoutingRequest) -> RoutingResponse[RoutingRequest]:
        return RoutingResponse(data=service.submit(normalize_id(lab_id), body))

    @router.get("/routing-labs/{lab_id}/requests", operation_id="listRoutingRequests")
    def requests(lab_id: UuidInput) -> RoutingResponse[list[RoutingRequest]]:
        return RoutingResponse(data=service.requests(normalize_id(lab_id)))

    @router.get("/routing-requests/{request_id}", operation_id="getRoutingRequest")
    def request(request_id: UuidInput) -> RoutingResponse[RoutingRequest]:
        return RoutingResponse(
            data=service.get("request", normalize_id(request_id), RoutingRequest)
        )

    @router.post("/routing-labs/{lab_id}/advance", operation_id="advanceRoutingLab")
    def advance(lab_id: UuidInput) -> RoutingResponse[RoutingLabMetrics]:
        return RoutingResponse(data=service.advance(normalize_id(lab_id)))

    @router.post("/routing-requests/{request_id}/resolve", operation_id="resolveRoutingRequest")
    def resolve(
        request_id: UuidInput, body: ResolveRoutingRequest
    ) -> RoutingResponse[RoutingRequest]:
        return RoutingResponse(data=service.resolve(normalize_id(request_id), body))

    @router.post("/routing-requests/{request_id}/feedback", operation_id="recordRoutingFeedback")
    def feedback(
        request_id: UuidInput, body: RoutingFeedbackRequest
    ) -> RoutingResponse[RoutingRequest]:
        return RoutingResponse(data=service.feedback(normalize_id(request_id), body))

    @router.post("/routing-labs/{lab_id}/candidates", operation_id="proposeRoutingRecipe")
    def propose(
        lab_id: UuidInput, body: ProposeRoutingRecipeRequest
    ) -> RoutingResponse[RoutingRecipe]:
        return RoutingResponse(data=service.propose(normalize_id(lab_id), body))

    @router.post("/routing-labs/{lab_id}/replays", operation_id="startRoutingReplay")
    def replay(
        lab_id: UuidInput, body: StartRoutingReplayRequest
    ) -> RoutingResponse[RoutingReplay]:
        return RoutingResponse(data=service.start_replay(normalize_id(lab_id), body))

    @router.get("/routing-replays/{replay_id}", operation_id="getRoutingReplay")
    def get_replay(replay_id: UuidInput) -> RoutingResponse[RoutingReplay]:
        return RoutingResponse(data=service.get("replay", normalize_id(replay_id), RoutingReplay))

    @router.post("/routing-recipes/{recipe_id}/publish", operation_id="publishRoutingRecipe")
    def publish(
        recipe_id: UuidInput, body: PublishRoutingRecipeRequest
    ) -> RoutingResponse[RoutingRecipe]:
        return RoutingResponse(data=service.publish(normalize_id(recipe_id), body))

    @router.post("/routing-recipes/{recipe_id}/pause", operation_id="pauseRoutingRecipe")
    def pause(
        recipe_id: UuidInput, body: PauseRoutingRecipeRequest
    ) -> RoutingResponse[RoutingRecipe]:
        return RoutingResponse(data=service.pause(normalize_id(recipe_id), body))

    @router.get("/routing-labs/{lab_id}/metrics", operation_id="getRoutingMetrics")
    def metrics(lab_id: UuidInput) -> RoutingResponse[RoutingLabMetrics]:
        return RoutingResponse(data=service.metrics(normalize_id(lab_id)))

    return router
