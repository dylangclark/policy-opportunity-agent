from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
from bs4 import BeautifulSoup

from ..classify import infer_event_type, infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, extract_first_date, parse_datetime
from .base import CollectResult
from .common import build_event

TIME_RE = re.compile(
    r"\b(1[0-2]|0?[1-9]):([0-5]\d)\s*(a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)


def _entry_datetime(entry: Any, timezone_name: str) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    for field in ("published", "updated", "created"):
        value = entry.get(field)
        if value:
            parsed = parse_datetime(value, assumed_timezone=timezone_name)
            if parsed:
                return parsed
    return None


def _plain_html(value: str | None) -> str:
    if not value:
        return ""
    return clean_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def _time_from_text(text: str, fallback: str) -> tuple[str, bool]:
    match = TIME_RE.search(text)
    if not match:
        return fallback, False
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = (match.group(3) or "").lower().replace(".", "")
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}", True


def _allowed(text: str, source: dict[str, Any]) -> bool:
    lowered = text.lower()
    includes = [str(value).lower() for value in source.get("include_keywords", [])]
    excludes = [str(value).lower() for value in source.get("exclude_keywords", [])]
    if includes and not any(value in lowered for value in includes):
        return False
    if excludes and any(value in lowered for value in excludes):
        return False
    return True


class RSSCollector:
    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        result = client.get(source["url"], conditional_key=source["id"])
        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        parsed = feedparser.parse(result.content)
        warnings: list[str] = []
        if parsed.bozo and parsed.bozo_exception:
            warnings.append(f"Feed parser warning: {parsed.bozo_exception}")

        events = []
        default_event_type = source.get("default_event_type", "policy_event")
        lifecycle = source.get("lifecycle", "published")
        timezone_name = source.get("timezone", "America/Toronto")
        date_semantics = source.get("date_semantics", "published")
        lookback = now - timedelta(days=int(source.get("lookback_days", 45)))
        lookahead = now + timedelta(days=int(source.get("lookahead_days", 365)))
        max_items = int(source.get("max_items", 250))

        for entry in parsed.entries[:max_items]:
            title = clean_text(entry.get("title", "Untitled item"))
            summary = _plain_html(entry.get("summary") or entry.get("description"))
            combined = clean_text(f"{title} {summary}")
            if not _allowed(combined, source):
                continue
            link = absolute_url(result.url, entry.get("link"))
            entry_date = _entry_datetime(entry, timezone_name)

            start_at = None
            published_at = entry_date
            all_day = False
            if date_semantics == "scheduled":
                extracted = extract_first_date(combined)
                if extracted:
                    hhmm, explicit_time = _time_from_text(combined, source.get("default_time", "09:00"))
                    start_at = combine_local_date(extracted, hhmm, timezone_name)
                    all_day = not explicit_time
                elif source.get("fallback_to_entry_date", False):
                    start_at = entry_date
                if start_at and not (lookback <= start_at <= lookahead):
                    continue
                lifecycle_value = "scheduled"
            else:
                if published_at and published_at < lookback:
                    continue
                lifecycle_value = lifecycle

            event_type = source.get("force_event_type") or infer_event_type(combined, default_event_type)
            topics = infer_topics(combined, source.get("topic_rules"))
            canonical = str(entry.get("id") or entry.get("guid") or link or f"{title}|{entry_date}")
            identifiers = {"feed_entry_id": str(entry.get("id") or entry.get("guid") or "")}
            identifiers = {key: value for key, value in identifiers.items() if value}

            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=link,
                    event_type=event_type,
                    lifecycle=lifecycle_value,
                    description=summary,
                    start_at=start_at,
                    published_at=published_at,
                    all_day=all_day,
                    topics=topics,
                    identifiers=identifiers,
                    raw={"tags": [tag.get("term") for tag in entry.get("tags", []) if tag.get("term")]},
                )
            )

        if not parsed.entries:
            warnings.append("Feed contained no entries.")
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )
