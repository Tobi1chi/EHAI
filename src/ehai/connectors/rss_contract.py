"""Provider-independent RSS and Atom feed data."""

from typing import Literal

from pydantic import Field

from ehai.application.connector_models import ConnectorModel


class FeedItem(ConnectorModel):
    item_id: str
    title: str | None
    link: str | None
    published_at: str | None
    updated_at: str | None
    author: str | None
    summary: str | None = Field(max_length=2000)


class FeedDocument(ConnectorModel):
    feed_url: str
    title: str | None
    link: str | None
    kind: Literal["rss", "atom"]
    fetched_at: str
    items: list[FeedItem]


class MergedFeedItem(FeedItem):
    feed_url: str
    feed_title: str | None


class FeedFailure(ConnectorModel):
    url: str
    reason: str


class FeedManyDocument(ConnectorModel):
    items: list[MergedFeedItem]
    failed: list[FeedFailure]
    fetched_at: str
