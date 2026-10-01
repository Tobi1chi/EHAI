"""A small host pump for opt-in labs; reuses Pi and the core's model admission."""

import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI

from ehai.infrastructure.routing_pi import PiRoutingFallback
from ehai.infrastructure.routing_projects import RoutingProjectDispatcher


def install_routing_fallback(
    app: FastAPI,
    fallback: PiRoutingFallback,
    *,
    project_dispatcher: RoutingProjectDispatcher | None = None,
) -> None:
    task: asyncio.Task[None] | None = None
    active: dict[str, asyncio.Task[None]] = {}
    service = fallback.service

    async def pump() -> None:
        try:
            while True:
                for identity, child in tuple(active.items()):
                    if child.done():
                        del active[identity]
                        child.result()
                for lab in service.labs.automatic_labs():
                    await asyncio.to_thread(service.labs.advance, lab.lab_id)
                    for record in service.labs.requests(lab.lab_id):
                        if record.fallback is not None:
                            await asyncio.to_thread(service.replay, record)
                            change = record.fallback.project_change
                            change_key = "project:" + record.request_id
                            if (
                                change is not None
                                and change.status == "pending"
                                and project_dispatcher is not None
                                and change_key not in active
                                and len(active) < fallback.capacity.capacity
                            ):
                                active[change_key] = asyncio.create_task(
                                    project_dispatcher.dispatch(record.request_id), name=change_key
                                )
                        elif (
                            record.status == "escalated"
                            and record.case_role == "learning"
                            and record.request_id not in active
                            and len(active) < fallback.capacity.capacity
                        ):
                            active[record.request_id] = asyncio.create_task(
                                fallback.execute(record.request_id),
                                name="ehai-routing-" + record.request_id,
                            )
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Fail visibly; do not repeatedly call a model after an unclassified host failure.
            logging.getLogger(__name__).error("Routing pump stopped: %s", type(error).__name__)
        finally:
            for child in active.values():
                child.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
            active.clear()

    async def start() -> None:
        nonlocal task
        service.recover()
        task = asyncio.create_task(pump(), name="ehai-routing-fallback")

    async def stop() -> None:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", stop)
