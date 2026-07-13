from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Event
from ..utils import clean_text, content_hash, stable_id, unique_preserve_order


def build_event(
    *,
    source: dict[str, Any],
    now: datetime,
    canonical_key: str,
    title: str,
    source_url: str | None = None,
    event_type: str = "policy_event",
    lifecycle: str = "published",
    description: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    published_at: datetime | None = None,
    all_day: bool = False,
    confidence: str = "confirmed",
    topics: list[str] | None = None,
    identifiers: dict[str, str] | None = None,
    metadata_only: bool = False,
    raw: dict[str, Any] | None = None,
) -> Event:
    clean_title = clean_text(title)
    clean_description = clean_text(description) or None
    payload = {
        "canonical_key": canonical_key,
        "title": clean_title,
        "description": clean_description,
        "event_type": event_type,
        "lifecycle": lifecycle,
        "start_at": start_at,
        "end_at": end_at,
        "published_at": published_at,
        "topics": sorted(topics or []),
        "identifiers": identifiers or {},
        "metadata_only": metadata_only,
        "source_url": source_url or source["url"],
    }
    return Event(
        id=f"{source['id']}:{stable_id(canonical_key)}",
        canonical_key=canonical_key,
        source_id=source["id"],
        source_name=source.get("name", source["id"]),
        source_url=source_url or source["url"],
        jurisdiction=source.get("jurisdiction", "CA"),
        institution=source.get("institution", source.get("name", source["id"])),
        event_type=event_type,
        lifecycle=lifecycle,
        title=clean_title,
        description=clean_description,
        start_at=start_at,
        end_at=end_at,
        published_at=published_at,
        source_timezone=source.get("timezone", "America/Toronto"),
        all_day=all_day,
        confidence=confidence,
        topics=unique_preserve_order(topics or []),
        identifiers=identifiers or {},
        metadata_only=metadata_only,
        first_seen_at=now,
        last_seen_at=now,
        content_hash=content_hash(payload),
        raw=raw or {},
    )
