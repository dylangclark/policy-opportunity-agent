from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, parse_date
from .base import CollectResult
from .common import build_event


class StatCanScheduleCollector:
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
        events = []
        lookback = int(source.get("lookback_days", 14))
        lookahead = int(source.get("lookahead_days", 180))
        minimum = now - timedelta(days=lookback)
        maximum = now + timedelta(days=lookahead)
        timezone_name = source.get("timezone", "America/Toronto")
        default_time = source.get("default_time", "08:30")

        for item in payload if isinstance(payload, list) else []:
            release_date = parse_date(str(item.get("date", "")))
            if release_date is None:
                continue
            start_at = combine_local_date(release_date, default_time, timezone_name)
            if start_at < minimum or start_at > maximum:
                continue
            title = clean_text(item.get("title") or "Statistics Canada release")
            description = clean_text(item.get("description")) or None
            item_url = absolute_url(result.url, item.get("url"))
            combined = f"{title} {description or ''}"
            canonical = f"statcan|{title.lower()}|{release_date.isoformat()}"
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=item_url,
                    event_type="economic_release",
                    lifecycle="scheduled",
                    description=description,
                    start_at=start_at,
                    topics=infer_topics(combined, source.get("topic_rules")),
                    identifiers={"release_date": release_date.isoformat()},
                    raw={"statcan_type": item.get("type")},
                )
            )

        return CollectResult(
            source_id=source["id"],
            events=events,
            status="ok",
            http_status=result.status_code,
        )
