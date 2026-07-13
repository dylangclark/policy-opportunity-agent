from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..classify import infer_event_type, infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, parse_datetime
from .base import CollectResult
from .common import build_event


class JSONFeedCollector:
    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        result = client.get(source["url"], conditional_key=source["id"])
        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        payload = result.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        events = []
        lookback = now - timedelta(days=int(source.get("lookback_days", 90)))
        includes = [str(value).lower() for value in source.get("include_keywords", [])]
        excludes = [str(value).lower() for value in source.get("exclude_keywords", [])]

        for item in items[: int(source.get("max_items", 250))]:
            title = clean_text(item.get("title") or "Untitled item")
            description_html = item.get("summary") or item.get("content_text") or item.get("content_html") or ""
            description = clean_text(BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True))
            combined = clean_text(f"{title} {description}")
            lowered = combined.lower()
            if includes and not any(keyword in lowered for keyword in includes):
                continue
            if excludes and any(keyword in lowered for keyword in excludes):
                continue
            item_url = absolute_url(result.url, item.get("url") or item.get("external_url"))
            published_at = parse_datetime(
                item.get("date_published") or item.get("date_modified"),
                assumed_timezone=source.get("timezone", "America/Vancouver"),
            )
            if published_at and published_at < lookback:
                continue
            event_type = source.get("force_event_type") or infer_event_type(
                combined,
                source.get("default_event_type", "policy_event"),
            )
            topics = infer_topics(combined, source.get("topic_rules"))
            canonical = str(item.get("id") or item_url or f"{title}|{published_at}")
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=item_url,
                    event_type=event_type,
                    lifecycle=source.get("lifecycle", "published"),
                    description=description,
                    published_at=published_at,
                    topics=topics,
                    identifiers={"feed_entry_id": str(item.get("id"))} if item.get("id") else {},
                    raw={"tags": item.get("tags", [])},
                )
            )
        warnings = [] if items else ["JSON feed contained no items."]
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )
