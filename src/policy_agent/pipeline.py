from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .collectors.base import CollectResult
from .collectors.registry import get_collector
from .config import AgentConfig, user_agent
from .http import HttpClient
from .models import Change, Event, Manifest, ManifestFile, SourceStatus
from .opportunities import identify_opportunities
from .utils import atomic_write_json, read_json, sha256_file, stable_id, utc_now

LOGGER = logging.getLogger(__name__)


def _load_previous_events(path: Path) -> list[Event]:
    payload = read_json(path, {})
    values = payload.get("events", []) if isinstance(payload, dict) else []
    events: list[Event] = []
    for value in values:
        try:
            events.append(Event.model_validate(value))
        except Exception as exc:
            LOGGER.warning("Ignoring invalid previous event: %s", exc)
    return events


def _changed_fields(old: Event, new: Event) -> list[str]:
    fields = [
        "title",
        "description",
        "event_type",
        "lifecycle",
        "start_at",
        "end_at",
        "published_at",
        "source_url",
        "topics",
        "identifiers",
        "metadata_only",
        "content_hash",
    ]
    changed: list[str] = []
    for field in fields:
        old_value = getattr(old, field)
        new_value = getattr(new, field)
        if str(old_value) != str(new_value):
            changed.append(field)
    return changed


def _calculate_changes(previous: list[Event], current: list[Event], now: datetime) -> list[Change]:
    old_map = {event.id: event for event in previous}
    new_map = {event.id: event for event in current}
    changes: list[Change] = []

    for event_id, event in new_map.items():
        old = old_map.get(event_id)
        if old is None:
            changes.append(
                Change(
                    event_id=event_id,
                    source_id=event.source_id,
                    change_type="new",
                    title=event.title,
                    detected_at=now,
                )
            )
        elif old.content_hash != event.content_hash:
            changes.append(
                Change(
                    event_id=event_id,
                    source_id=event.source_id,
                    change_type="changed",
                    title=event.title,
                    detected_at=now,
                    changed_fields=_changed_fields(old, event),
                )
            )

    for event_id, event in old_map.items():
        if event_id not in new_map:
            changes.append(
                Change(
                    event_id=event_id,
                    source_id=event.source_id,
                    change_type="removed",
                    title=event.title,
                    detected_at=now,
                )
            )

    order = {"changed": 0, "new": 1, "removed": 2}
    changes.sort(key=lambda item: (order[item.change_type], item.source_id, item.title.lower()))
    return changes


def _retain_event(event: Event, now: datetime, settings: dict[str, Any]) -> bool:
    past_days = int(settings.get("event_retention_days", 180))
    future_days = int(settings.get("future_retention_days", 365))
    undated_days = int(settings.get("undated_retention_days", 30))
    lower = now - timedelta(days=past_days)
    upper = now + timedelta(days=future_days)
    dates = [value for value in (event.start_at, event.end_at, event.published_at) if value]
    if dates:
        return any(lower <= value <= upper for value in dates) or any(value >= now for value in dates)
    return event.first_seen_at >= now - timedelta(days=undated_days)


def _has_usable_date(event: Event) -> bool:
    """Return True only when an event has a real source-derived date."""
    return any(
        value is not None
        for value in (
            event.start_at,
            event.end_at,
            event.published_at,
        )
    )


def _normalized_title(value: str) -> str:
    """Normalize titles for conservative duplicate matching."""
    value = value.lower()
    value = re.sub(r"\\b(?:updated?|new|notice of)\\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\\s+", " ", value).strip()


def _event_date_key(event: Event) -> str:
    """Use the most operationally relevant date as the duplicate date."""
    value = event.start_at or event.published_at or event.end_at
    return value.date().isoformat() if value else ""


def _event_quality(event: Event) -> tuple[int, int, int, int, int]:
    """Rank duplicate candidates without changing their substantive content."""
    confidence_score = {
        "confirmed": 3,
        "expected": 2,
        "inferred": 1,
    }.get(event.confidence, 0)

    direct_document = int(
        any(
            token in str(event.source_url).lower()
            for token in (
                "/document/",
                ".pdf",
                "/decision/",
                "/order/",
                "/report/",
            )
        )
    )

    return (
        confidence_score,
        int(event.start_at is not None),
        int(event.published_at is not None),
        direct_document,
        len(event.description or ""),
    )


def _deduplicate_events(events: list[Event]) -> tuple[list[Event], int]:
    """
    Remove exact and conservative cross-source duplicates.

    Cross-source matching requires the same normalized title, jurisdiction,
    event type, and calendar date. This avoids merging related but distinct
    milestones.
    """
    exact_map: dict[str, Event] = {}

    for event in events:
        existing = exact_map.get(event.id)
        if existing is None or _event_quality(event) > _event_quality(existing):
            exact_map[event.id] = event

    grouped: dict[tuple[str, str, str, str], Event] = {}

    for event in exact_map.values():
        key = (
            _normalized_title(event.title),
            event.jurisdiction.lower().strip(),
            event.event_type.lower().strip(),
            _event_date_key(event),
        )

        existing = grouped.get(key)
        if existing is None or _event_quality(event) > _event_quality(existing):
            grouped[key] = event

    retained = list(grouped.values())
    removed = len(events) - len(retained)
    return retained, removed


def _sort_events(events: list[Event]) -> list[Event]:
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(
        events,
        key=lambda event: (
            event.start_at or event.end_at or event.published_at or far_future,
            event.source_id,
            event.title.lower(),
        ),
    )


def _dump_items(items: list[Any]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json", exclude_none=True) for item in items]


def run_pipeline(
    *,
    agent_config: AgentConfig,
    rules: dict[str, Any],
    output_dir: Path,
    state_dir: Path,
    now: datetime | None = None,
) -> Manifest:
    now = (now or utc_now()).astimezone(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{stable_id(now.isoformat(), length=8)}"

    previous_events = _load_previous_events(output_dir / "events.json")
    previous_by_source: dict[str, list[Event]] = {}
    for event in previous_events:
        previous_by_source.setdefault(event.source_id, []).append(event)
    previous_map = {event.id: event for event in previous_events}

    merged_events: list[Event] = []
    statuses: list[SourceStatus] = []

    with HttpClient(
        state_dir,
        user_agent=user_agent(agent_config.settings),
        timeout_seconds=float(agent_config.settings.get("http_timeout_seconds", 30)),
        retries=int(agent_config.settings.get("http_retries", 2)),
        min_host_interval_seconds=float(agent_config.settings.get("min_host_interval_seconds", 0.35)),
    ) as client:
        for source in agent_config.sources:
            previous_source_events = previous_by_source.get(source["id"], [])
            if not source.get("enabled", True):
                statuses.append(
                    SourceStatus(
                        source_id=source["id"],
                        source_name=source["name"],
                        collector=source["collector"],
                        url=source["url"].format(year=now.year),
                        status="disabled",
                        checked_at=now,
                    )
                )
                continue

            try:
                collector = get_collector(source["collector"])
                result: CollectResult = collector.collect(source, client, now)
            except Exception as exc:
                LOGGER.exception("Collector %s failed", source["id"])
                result = CollectResult(
                    source_id=source["id"],
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

            retained_previous = 0
            if (
                result.status == "ok"
                and not result.events
                and previous_source_events
                and source.get("retain_on_empty", True)
            ):
                result.status = "partial"
                result.warnings.append("Collector returned no events; retained the last good source data.")

            if result.status == "ok":
                selected = result.events
                selected_ids = {event.id for event in selected}
            elif result.status == "not_modified" or result.not_modified:
                selected = previous_source_events
                selected_ids = set()
                retained_previous = len(selected)
            elif result.status == "partial":
                selected = list(result.events)
                selected_ids = {event.id for event in selected}
                for old_event in previous_source_events:
                    if old_event.id not in selected_ids:
                        selected.append(old_event)
                        retained_previous += 1
            else:
                selected = previous_source_events
                selected_ids = set()
                retained_previous = len(selected)

            for event in selected:
                old = previous_map.get(event.id)
                if old:
                    event.first_seen_at = old.first_seen_at
                if event.id in selected_ids or result.status in {"ok", "not_modified"}:
                    event.last_seen_at = now
                merged_events.append(event)

            status_value = result.status if result.status in {"ok", "not_modified", "partial", "failed"} else "failed"
            status_url = source["url"].format(year=now.year)
            statuses.append(
                SourceStatus(
                    source_id=source["id"],
                    source_name=source["name"],
                    collector=source["collector"],
                    url=status_url,
                    status=status_value,
                    checked_at=now,
                    event_count=len(result.events),
                    retained_previous_count=retained_previous,
                    http_status=result.http_status,
                    error=result.error,
                    warnings=result.warnings,
                    stale=status_value in {"partial", "failed"},
                )
            )

    retained_events = [
        event
        for event in merged_events
        if _retain_event(event, now, agent_config.settings)
    ]

    undated_count = 0
    if agent_config.settings.get("drop_undated_events", True):
        dated_events = [
            event
            for event in retained_events
            if _has_usable_date(event)
        ]
        undated_count = len(retained_events) - len(dated_events)
        retained_events = dated_events

    duplicate_count = 0
    if agent_config.settings.get("deduplicate_events", True):
        retained_events, duplicate_count = _deduplicate_events(
            retained_events
        )

    if undated_count:
        LOGGER.info(
            "Dropped %s events with no usable source date",
            undated_count,
        )

    if duplicate_count:
        LOGGER.info(
            "Dropped %s duplicate events",
            duplicate_count,
        )

    current_events = _sort_events(retained_events)
    changes = _calculate_changes(previous_events, current_events, now)
    opportunities = identify_opportunities(current_events, changes, now, agent_config.settings, rules)

    status_counts = Counter(status.status for status in statuses)
    if status_counts["failed"] and status_counts["ok"] + status_counts["not_modified"] + status_counts["partial"] == 0:
        run_status = "failed"
    elif status_counts["failed"] or status_counts["partial"]:
        run_status = "partial"
    else:
        run_status = "ok"

    envelopes = {
        "events.json": {
            "schema_version": "1.0",
            "generated_at": now.isoformat(),
            "run_id": run_id,
            "events": _dump_items(current_events),
        },
        "opportunities.json": {
            "schema_version": "1.0",
            "generated_at": now.isoformat(),
            "run_id": run_id,
            "opportunities": _dump_items(opportunities),
        },
        "changes.json": {
            "schema_version": "1.0",
            "generated_at": now.isoformat(),
            "run_id": run_id,
            "changes": _dump_items(changes),
        },
        "source-status.json": {
            "schema_version": "1.0",
            "generated_at": now.isoformat(),
            "run_id": run_id,
            "sources": _dump_items(statuses),
        },
        "heartbeat.json": {
            "schema_version": "1.0",
            "generated_at": now.isoformat(),
            "run_id": run_id,
            "status": run_status,
            "source_summary": dict(status_counts),
        },
    }

    for filename, payload in envelopes.items():
        atomic_write_json(output_dir / filename, payload)

    file_counts = {
        "events.json": len(current_events),
        "opportunities.json": len(opportunities),
        "changes.json": len(changes),
        "source-status.json": len(statuses),
        "heartbeat.json": None,
    }
    manifest_files: dict[str, ManifestFile] = {}
    for filename in envelopes:
        path = output_dir / filename
        manifest_files[filename.removesuffix(".json").replace("-", "_")] = ManifestFile(
            path=filename,
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            count=file_counts[filename],
        )

    manifest = Manifest(
        generated_at=now,
        run_id=run_id,
        status=run_status,
        timezone=agent_config.settings.get("display_timezone", "America/Vancouver"),
        horizons={
            "execution_days": int(agent_config.settings.get("execution_days", 14)),
            "preparation_days": int(agent_config.settings.get("preparation_days", 30)),
            "scouting_days": int(agent_config.settings.get("scouting_days", 90)),
            "lookback_days": int(agent_config.settings.get("lookback_days", 14)),
        },
        files=manifest_files,
        counts={
            "events": len(current_events),
            "opportunities": len(opportunities),
            "changes": len(changes),
            "sources": len(statuses),
        },
        source_summary=dict(status_counts),
    )
    atomic_write_json(output_dir / "manifest.json", manifest.model_dump(mode="json", exclude_none=True))
    return manifest
