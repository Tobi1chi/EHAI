"""Bounded HTTP fetching and side-effect-free RSS 2.0 / Atom 1.0 parsing."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

from ehai.connectors.rss_contract import FeedDocument, FeedItem

_ATOM = "http://www.w3.org/2005/Atom"
_DC = "http://purl.org/dc/elements/1.1/"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"


class FeedError(ValueError):
    """A feed could not be safely fetched or parsed."""


class FeedParseError(FeedError):
    """The response is invalid, unsafe XML or is not a supported feed."""


class FeedFetchError(FeedError):
    """HTTP transport, status, redirect or size failure."""


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    return "".join(element.itertext()).strip() or None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
        # RFC 822 -0000 means UTC, though email.utils returns a naive value.
        if parsed.tzinfo is None and re.search(r"\s-0000\s*$", value):
            parsed = parsed.replace(tzinfo=UTC)
    # A timezone-less date has no unambiguous UTC representation.
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _summary(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    # Serialize actual XHTML children; escaped HTML is already decoded by XML.
    value = (element.text or "") + "".join(
        ET.tostring(child, encoding="unicode") for child in element
    )
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())[:2000] or None


def _link(element: ET.Element, prefix: str, feed_url: str) -> str | None:
    for link in element.findall(prefix + "link"):
        if prefix:
            if link.get("rel", "alternate") != "alternate":
                continue
            value = link.get("href")
        else:
            value = _text(link)
        if value:
            return urljoin(feed_url, value.strip())
    return None


def _xml_content(content: bytes) -> str:
    """Normalize transport text without allowing XML declarations to hide."""
    raw = content.lstrip(b" \t\r\n")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif raw.startswith(b"\x00\x00\x00<"):
        encoding = "utf-32-be"
    elif raw.startswith(b"<\x00\x00\x00"):
        encoding = "utf-32-le"
    elif raw.startswith(b"\x00<"):
        encoding = "utf-16-be"
    elif raw.startswith(b"<\x00"):
        encoding = "utf-16-le"
    else:
        raw = raw.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
        declaration = re.match(rb"<\?xml\s+[^?]*\?>", raw)
        declared = (
            re.search(rb"\bencoding\s*=\s*(['\"])([^'\"]+)\1", declaration[0])
            if declaration
            else None
        )
        try:
            encoding = declared[2].decode("ascii") if declared else "utf-8"
        except UnicodeError as exc:
            raise FeedParseError("Invalid feed XML encoding") from exc
    try:
        text = raw.decode(encoding).lstrip("\ufeff \t\r\n")
    except (LookupError, UnicodeError, ValueError, TypeError) as exc:
        raise FeedParseError("Invalid or unsupported feed XML encoding") from exc
    # Only inspect the document prologue/root, never HTML embedded in summaries.
    start = re.sub(r"\A(?:<\?xml\s+.*?\?>\s*)?(?:<!--.*?-->\s*)*", "", text, flags=re.S)
    if re.match(r"(?:<!DOCTYPE\s+html\b|<(?:html|head|body)\b)", start, re.I):
        raise FeedParseError("response is HTML, not a feed")
    # Check both representations, including UTF-16/32 and codec-decoded text.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I) or re.search(
        rb"<!\s*(?:DOCTYPE|ENTITY)\b", content.replace(b"\x00", b""), re.I
    ):
        raise FeedParseError("DOCTYPE and ENTITY declarations are not permitted")
    return text


def parse_feed(content: bytes, feed_url: str, fetched_at: str | datetime) -> FeedDocument:
    """Parse a whole feed; invalid or unsafe documents never produce partial output.

    Dates without a timezone or with invalid syntax become null. fetched_at must
    be an aware datetime or an ISO-8601 timestamp with a timezone.
    """
    normalized = _xml_content(content)
    try:
        root = ET.fromstring(normalized)
    except (ET.ParseError, ValueError, LookupError) as exc:
        raise FeedParseError("Malformed feed XML") from exc
    prefix = ""
    kind: Literal["rss", "atom"]
    if root.tag == "rss" and root.get("version") == "2.0":
        channel = root.find("channel")
        if channel is None:
            raise FeedParseError("RSS feed has no channel")
        kind = "rss"
        entries = channel.findall("item")
    elif root.tag == f"{{{_ATOM}}}feed":
        prefix = f"{{{_ATOM}}}"
        channel = root
        kind = "atom"
        entries = channel.findall(prefix + "entry")
    else:
        raise FeedParseError("Response is not an RSS 2.0 or Atom 1.0 feed")
    fetched = _date(fetched_at.isoformat() if isinstance(fetched_at, datetime) else fetched_at)
    if fetched is None:
        raise FeedParseError("fetched_at must be a timestamp with a timezone")
    items: list[FeedItem] = []
    for entry in entries:
        title = _text(entry.find(prefix + "title"))
        link = _link(entry, prefix, feed_url)
        published = _text(entry.find(prefix + ("published" if prefix else "pubDate")))
        identifier = _text(entry.find(prefix + ("id" if prefix else "guid")))
        summary = entry.find(prefix + ("summary" if prefix else "description"))
        if summary is None and prefix:
            summary = entry.find(prefix + "content")
        elif summary is None:
            summary = entry.find(f"{{{_CONTENT}}}encoded")
        author = _text(entry.find(prefix + "author" + ("/" + prefix + "name" if prefix else "")))
        if not prefix and entry.find("author") is None:
            author = _text(entry.find(f"{{{_DC}}}creator"))
        items.append(
            FeedItem(
                item_id=identifier
                or link
                or hashlib.sha256(((title or "") + (published or "")).encode("utf-8")).hexdigest(),
                title=title,
                link=link,
                published_at=_date(published),
                updated_at=_date(_text(entry.find(prefix + "updated"))) if prefix else None,
                author=author,
                summary=_summary(summary),
            )
        )
    return FeedDocument(
        feed_url=feed_url,
        title=_text(channel.find(prefix + "title")),
        link=_link(channel, prefix, feed_url),
        kind=kind,
        fetched_at=fetched,
        items=items,
    )


def fetch_feed(
    client: httpx.Client,
    feed_url: str,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    timeout: float | httpx.Timeout = 30.0,
) -> FeedDocument:
    """Stream at most max_bytes decoded bytes, following at most five redirects."""
    if max_bytes < 1:
        raise FeedFetchError("max_bytes must be positive")
    url = feed_url
    try:
        for redirects in range(6):
            parsed = httpx.URL(url)
            if parsed.scheme not in {"http", "https"} or not parsed.host:
                raise FeedFetchError("Feed URL must use HTTP or HTTPS")
            with client.stream("GET", url, follow_redirects=False, timeout=timeout) as response:
                if response.status_code >= 400:
                    raise FeedFetchError(f"Feed returned HTTP {response.status_code}")
                if response.is_redirect:
                    location = response.headers.get("location")
                    if redirects == 5 or not location:
                        raise FeedFetchError("Feed redirect limit exceeded or missing location")
                    url = urljoin(str(response.url), location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise FeedFetchError(f"Unexpected feed HTTP {response.status_code}")
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > max_bytes:
                        raise FeedFetchError("Feed exceeds maximum byte size")
                    content.extend(chunk)
                return parse_feed(bytes(content), feed_url, datetime.now(UTC))
    except httpx.TimeoutException as exc:
        raise FeedFetchError("Feed request timed out") from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise FeedFetchError("Feed HTTP request failed") from exc
    raise FeedFetchError("Feed redirect limit exceeded")
