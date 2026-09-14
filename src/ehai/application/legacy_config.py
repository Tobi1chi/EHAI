"""Read-only configuration vocabulary for historic Built-in execution records."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResponsesEndpointCapabilities:
    """Explicit Responses features guaranteed by one configured provider endpoint."""

    supports_background: bool = True
    supports_idempotent_create: bool | None = None
    supports_unique_items: bool = True
    supports_previous_response_id: bool = True
    supports_response_retrieval: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.supports_background, bool):
            raise ValueError("supports_background must be a boolean")
        if self.supports_idempotent_create is not None and not isinstance(
            self.supports_idempotent_create, bool
        ):
            raise ValueError("supports_idempotent_create must be a boolean")
        if not isinstance(self.supports_unique_items, bool):
            raise ValueError("supports_unique_items must be a boolean")
        for name in ("supports_previous_response_id", "supports_response_retrieval"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
