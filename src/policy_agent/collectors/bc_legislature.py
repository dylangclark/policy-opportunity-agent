from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..classify import infer_event_type, infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, parse_datetime
from .base import CollectResult
from .common import build_event


class BCLegislatureCalendarCollector:
    """Parse FullCalendar events embedded in the Legislature's Drupal settings."""

    def collect(
        self,
        source: dict[str, Any],
        client: HttpClient,
        now: datetime,
    ) -> CollectResult:
        result = client.get(source["url"], conditional_key=source["id"])

        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        soup = BeautifulSoup(result.content, "html.parser")
        settings_node = soup.find(
            "script",
            attrs={"data-drupal-selector": "drupal-settings-json"},
        )

        if settings_node is None:
            return CollectResult(
                source_id=source["id"],
                status="partial",
                http_status=result.status_code,
                warnings=["Drupal settings JSON was not found."],
            )

        try:
            settings = json.loads(settings_node.get_text())
            views = settings.get("fullCalendarView") or []
            calendar_options_raw = views[0]["calendar_options"]
            calendar_options = json.loads(calendar_options_raw)
            source_events = calendar_options.get("events") or []
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            return CollectResult(
                source_id=source["id"],
                status="partial",
                http_status=result.status_code,
                warnings=[f"Could not parse embedded FullCalendar data: {exc}"],
            )

        timezone_name = source.get("timezone", "America/Vancouver")
        earliest = now - timedelta(days=int(source.get("lookback_days", 7)))
        latest = now + timedelta(days=int(source.get("lookahead_days", 365)))

        include_titles = {
            clean_text(str(value)).lower()
            for value in source.get(
                "include_titles",
                [
                    "In Session",
                    "Budget Day",
                    "Speech from the Throne",
                    "Speech From the Throne",
                ],
            )
        }

        events = []
        skipped_invalid = 0

        for item in source_events:
            title = clean_text(str(item.get("title") or ""))

            if not title or title.lower() not in include_titles:
                continue

            start_text = item.get("start")
            if not start_text:
                skipped_invalid += 1
                continue

            start_at = parse_datetime(
                str(start_text),
                assumed_timezone=timezone_name,
            )
            end_at = (
                parse_datetime(
                    str(item["end"]),
                    assumed_timezone=timezone_name,
                )
                if item.get("end")
                else None
            )

            if start_at is None:
                skipped_invalid += 1
                continue

            if start_at < earliest or start_at > latest:
                continue

            node_url = absolute_url(result.url, item.get("url"))
            event_id = str(item.get("eid") or item.get("id") or start_text)
            canonical = f"bc-leg-calendar|{event_id}"

            if title.lower() == "in session":
                event_type = "legislative_sitting"
                description = "B.C. Legislative Assembly scheduled to be in session."
            elif title.lower() == "budget day":
                event_type = "fiscal_release"
                description = "B.C. provincial Budget Day."
            elif "throne" in title.lower():
                event_type = "speech"
                description = "Speech from the Throne at the B.C. Legislative Assembly."
            else:
                event_type = infer_event_type(
                    title,
                    source.get("default_event_type", "policy_event"),
                )
                description = title

            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=node_url,
                    event_type=event_type,
                    lifecycle=source.get("lifecycle", "scheduled"),
                    description=description,
                    start_at=start_at,
                    end_at=end_at,
                    all_day=True,
                    confidence=source.get("confidence", "confirmed"),
                    topics=infer_topics(title, source.get("topic_rules")),
                    identifiers={
                        "legislature_event_id": event_id,
                    },
                    raw={
                        "embedded_title": title,
                        "embedded_start": start_text,
                        "embedded_end": item.get("end"),
                        "node_id": item.get("eid"),
                        "background_color": item.get("backgroundColor"),
                    },
                )
            )

        warnings = []

        if skipped_invalid:
            warnings.append(
                f"Skipped {skipped_invalid} calendar entries with invalid dates."
            )

        if not events:
            warnings.append(
                "No Legislature calendar events were found within the configured horizon."
            )

        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )
