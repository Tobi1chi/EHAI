"""The one idempotency mechanism: a receipt per (scope, key) replays the recorded result.

Core Commands, life Workflows, Connectors and the routing lab all look up and record their
receipts here, inside the transaction that performs the command. Each caller keeps its own
key namespace (``scope``) and decides which error a reused key raises.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from hashlib import sha256

from ehai import JsonValue, json_dumps
from ehai.application.ports import COMMAND_RECEIPT_SCOPE, CommandReceipt, CommandReceiptStore


def content_fingerprint(content: JsonValue) -> str:
    """Return the fingerprint of a command's canonical JSON content."""
    return sha256(json_dumps(content).encode()).hexdigest()


def recorded_result(
    receipts: CommandReceiptStore,
    idempotency_key: str,
    command_name: str,
    fingerprint: str,
    *,
    conflict: Callable[[CommandReceipt], Exception],
    scope: str = COMMAND_RECEIPT_SCOPE,
) -> dict[str, JsonValue] | None:
    """Return the result recorded for this exact command, or ``None`` if the key is unused.

    A key already used for another command or other content raises ``conflict(receipt)``
    before the caller performs any side effect.
    """
    receipt = receipts.get(idempotency_key, scope=scope)
    if receipt is None:
        return None
    if receipt.command_name != command_name or receipt.command_fingerprint != fingerprint:
        raise conflict(receipt)
    return receipt.result


def record_result(
    receipts: CommandReceiptStore,
    idempotency_key: str,
    command_name: str,
    fingerprint: str,
    result: Mapping[str, JsonValue],
    *,
    created_at: datetime,
    scope: str = COMMAND_RECEIPT_SCOPE,
) -> None:
    """Record a command's result in the caller's transaction."""
    receipts.put(
        CommandReceipt(
            scope=scope,
            idempotency_key=idempotency_key,
            command_name=command_name,
            command_fingerprint=fingerprint,
            result=result,
            created_at=created_at,
        )
    )
