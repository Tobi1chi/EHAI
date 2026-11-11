"""Independent read-only RSS connector using the durable HTTP bridge."""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import Field, ValidationError, field_validator

from ehai.application.connector_models import (
    ConnectorAction,
    ConnectorClaim,
    ConnectorManifest,
    ConnectorModel,
)
from ehai.connectors.bridge import ConnectorOutcome, _api_base, _initialize, _lock, _read, _run_once
from ehai.connectors.rss import FeedError, fetch_feed
from ehai.connectors.rss_contract import (
    FeedDocument,
    FeedFailure,
    FeedManyDocument,
    MergedFeedItem,
)


class FeedFetchInput(ConnectorModel):
    feed_url: str = Field(strict=True, min_length=1)
    max_items: int = Field(default=50, strict=True, ge=1, le=200)


class FeedFetchManyInput(ConnectorModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    max_items_per_feed: int = Field(strict=True, ge=1, le=100)

    @field_validator("urls")
    @classmethod
    def valid_urls(cls, urls: list[str]) -> list[str]:
        _feed_urls(urls)
        return urls


def rss_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_type="rss",
        version="1",
        actions=[
            ConnectorAction(
                name="feed.fetch",
                version="1",
                read_only=True,
                description="Fetch an allowed RSS/Atom feed, newest items first",
                input_schema=FeedFetchInput.model_json_schema(),
                output_schema=FeedDocument.model_json_schema(),
            ),
            ConnectorAction(
                name="feed.fetch_many",
                version="1",
                read_only=True,
                description="Fetch multiple allowed RSS/Atom feeds, newest items first",
                input_schema=FeedFetchManyInput.model_json_schema(),
                output_schema=FeedManyDocument.model_json_schema(),
            ),
        ],
    )


def _feed_urls(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("Configure at least one allowed feed URL")
    urls: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError("Feed URL must be text")
        parsed = httpx.URL(entry)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("Feed URL must use HTTP or HTTPS")
        urls.append(entry)
    return tuple(urls)


class RssConnectorAdapter:
    def __init__(self, client: httpx.Client, allowed_feed_urls: tuple[str, ...]) -> None:
        self.client = client
        self.allowed_feed_urls = allowed_feed_urls

    def execute(self, claim: ConnectorClaim) -> ConnectorOutcome:
        manifest = rss_manifest()
        # Connections registered before fetch_many keep their original immutable manifest.
        legacy_manifest = manifest.model_copy(update={"actions": manifest.actions[:1]})
        if (
            claim.connection.manifest not in (manifest, legacy_manifest)
            or claim.call.action not in claim.connection.manifest.actions
            or claim.connection.configuration.get("allowed_feed_urls")
            != list(self.allowed_feed_urls)
        ):
            return ConnectorOutcome("failed", error="RSS contract does not match this connection")
        try:
            if claim.call.action.name == "feed.fetch_many":
                return self._fetch_many(FeedFetchManyInput.model_validate(claim.call.inputs))
            request = FeedFetchInput.model_validate(claim.call.inputs)
            if request.feed_url not in self.allowed_feed_urls:
                return ConnectorOutcome(
                    "failed", error="Feed URL is not allowed by this connection"
                )
            document = fetch_feed(self.client, request.feed_url, timeout=30.0)
            # Compare instants, not ISO strings (fractional seconds may differ).
            # Python's stable sort retains equal-date and undated document order.
            oldest = datetime.min.replace(tzinfo=UTC)
            items = sorted(
                document.items,
                key=lambda item: (
                    bool(item.published_at or item.updated_at),
                    datetime.fromisoformat(item.published_at or item.updated_at or "")
                    if item.published_at or item.updated_at
                    else oldest,
                ),
                reverse=True,
            )
            result = document.model_copy(update={"items": items[: request.max_items]})
            return ConnectorOutcome("completed", output=result.model_dump(mode="json"))
        except FeedError as error:
            # FeedError messages are controlled by rss.py, without URLs or bodies.
            return ConnectorOutcome("failed", error=str(error))
        except ValidationError as error:
            details = []
            for issue in error.errors(
                include_url=False, include_input=False, include_context=False
            )[:3]:
                # Extra input keys can contain private text; never echo them.
                field = issue["loc"][0] if issue["loc"] else None
                label = (
                    field
                    if field in {"feed_url", "max_items", "urls", "max_items_per_feed"}
                    else "inputs"
                )
                details.append(f"{label}: {issue['msg']}")
            return ConnectorOutcome(
                "failed", error=f"Invalid {claim.call.action.name} inputs: " + "; ".join(details)
            )
        except Exception:
            # Read-only failures (including recovery/reconcile) are never unknown.
            # Do not expose feed credentials or response content in bridge errors.
            return ConnectorOutcome("failed", error="RSS fetch or input validation failed")

    def _fetch_many(self, request: FeedFetchManyInput) -> ConnectorOutcome:
        def fetch(url: str) -> FeedDocument | FeedFailure:
            if url not in self.allowed_feed_urls:
                return FeedFailure(url=url, reason="Feed URL is not allowed by this connection")
            try:
                return fetch_feed(self.client, url, timeout=30.0)
            except FeedError as error:
                return FeedFailure(url=url, reason=str(error))
            except Exception:
                return FeedFailure(url=url, reason="RSS fetch or input validation failed")

        with ThreadPoolExecutor(max_workers=8) as pool:
            documents = list(pool.map(fetch, request.urls))
        failed: list[FeedFailure] = []
        items: list[MergedFeedItem] = []
        oldest = datetime.min.replace(tzinfo=UTC)

        def instant(item: MergedFeedItem) -> datetime:
            value = item.published_at or item.updated_at
            return datetime.fromisoformat(value) if value else oldest

        for document in documents:
            if isinstance(document, FeedFailure):
                failed.append(document)
                continue
            feed_items = [
                MergedFeedItem(
                    **item.model_dump(), feed_url=document.feed_url, feed_title=document.title
                )
                for item in document.items
            ]
            items.extend(
                sorted(feed_items, key=instant, reverse=True)[: request.max_items_per_feed]
            )
        if len(failed) == len(documents):
            return ConnectorOutcome("failed", error="Every feed failed to fetch")
        output = FeedManyDocument(
            items=sorted(items, key=instant, reverse=True),
            failed=failed,
            fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        return ConnectorOutcome("completed", output=output.model_dump(mode="json"))


def _run(directory: Path, *, once: bool) -> None:
    config = _read(directory / "bridge.json")
    token = _read(directory / "bridge-token.json")["token"]
    registration = _read(directory / "registration.json")
    allowed = _feed_urls(registration["request"]["configuration"]["allowed_feed_urls"])
    with (
        httpx.Client(
            base_url=_api_base(config["api_url"]),
            headers={"X-EHAI-Connector-Token": token},
            timeout=30,
            trust_env=False,
        ) as core,
        httpx.Client(timeout=30, follow_redirects=False) as provider,
    ):
        adapter = RssConnectorAdapter(provider, allowed)
        while True:
            _run_once(directory, core, adapter, config)
            if once:
                return
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent RSS/Atom connector")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--api-url", required=True)
    init.add_argument("--project-id", required=True)
    init.add_argument("--feed-url", action="append", required=True)
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    for command in (init, run):
        command.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with _lock(args.state_dir):
            if args.command == "init":
                allowed = _feed_urls(args.feed_url)
                _initialize(
                    args.state_dir,
                    args.api_url,
                    args.project_id,
                    rss_manifest(),
                    {"allowed_feed_urls": list(allowed)},
                    "RSS feeds",
                )
            else:
                _run(args.state_dir, once=args.once)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            f"RSS connector stopped ({type(error).__name__}); private state retained",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
