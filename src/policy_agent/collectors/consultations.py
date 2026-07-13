from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, normalize_key, parse_date
from .base import CollectResult
from .common import build_event


def _normalized_row(row: dict[str, str]) -> dict[str, str]:
    return {normalize_key(key): clean_text(value) for key, value in row.items() if key}


def _first(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        normalized = normalize_key(key)
        if row.get(normalized):
            return row[normalized]
    return ""


class FederalConsultationsCollector:
    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        result = client.get(source["url"], conditional_key=source["id"])
        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        text = result.content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        events = []
        timezone_name = source.get("timezone", "America/Toronto")
        lookback = now - timedelta(days=int(source.get("lookback_days", 14)))
        lookahead = now + timedelta(days=int(source.get("lookahead_days", 365)))

        for source_row in reader:
            row = _normalized_row(source_row)
            title = _first(row, "Consultation Title (English)", "Consultation Title", "Title English", "Title")
            if not title:
                continue
            status = _first(row, "Status").lower()
            start_date = parse_date(_first(row, "Start Date"))
            end_date = parse_date(_first(row, "End Date"))
            start_at = combine_local_date(start_date, "00:00", timezone_name) if start_date else None
            end_at = (
                combine_local_date(end_date, source.get("default_time", "23:59"), timezone_name) if end_date else None
            )

            is_open = any(token in status for token in ("open", "accepting", "upcoming", "planned", "ongoing"))
            within_window = bool(
                (end_at and end_at >= lookback and end_at <= lookahead)
                or (start_at and start_at >= lookback and start_at <= lookahead)
            )
            if not is_open and not within_window:
                continue
            if end_at and end_at < lookback:
                continue

            registration = _first(row, "Registration Number")
            institution = _first(row, "Lead Department", "Department", "Organization")
            description = _first(row, "Description (English)", "Description English", "Description")
            item_url = (
                _first(
                    row,
                    "Link to Consultations Profile Page (English)",
                    "Consultation URL English",
                    "URL English",
                    "URL",
                )
                or source["url"]
            )
            subjects = _first(row, "Subjects", "Subject")
            high_profile = _first(row, "High Profile")
            combined = f"{title} {description} {subjects} {institution}"
            canonical = registration or f"federal-consultation|{title}|{start_date}|{end_date}"
            lifecycle = "open" if is_open else "closed"

            event_source = dict(source)
            if institution:
                event_source["institution"] = institution
            events.append(
                build_event(
                    source=event_source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=absolute_url(result.url, item_url),
                    event_type="consultation",
                    lifecycle=lifecycle,
                    description=description or None,
                    start_at=start_at,
                    end_at=end_at,
                    all_day=True,
                    topics=infer_topics(combined, source.get("topic_rules")),
                    identifiers={"registration_number": registration} if registration else {},
                    raw={
                        "status": status,
                        "subjects": subjects,
                        "high_profile": high_profile,
                        "department": institution,
                    },
                )
            )

        warnings = []
        if reader.fieldnames is None:
            warnings.append("Consultations CSV had no header row.")
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )


OPEN_UNTIL_RE = re.compile(
    r"Engagement\s+(?:is|was)\s+(?:open\s+)?until\s+([^.;|]+)",
    re.IGNORECASE,
)


class GovTogetherCollector:
    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        events = []
        warnings: list[str] = []
        timezone_name = source.get("timezone", "America/Vancouver")
        max_pages = int(source.get("max_pages", 5))
        next_url: str | None = source["url"]
        visited: set[str] = set()
        seen_items: set[str] = set()
        last_status: int | None = None

        for _ in range(max_pages):
            if not next_url or next_url in visited:
                break
            visited.add(next_url)
            try:
                response = client.get(next_url, allow_not_modified=False)
                last_status = response.status_code
            except Exception as exc:
                warnings.append(f"govTogether page failed: {type(exc).__name__}: {exc}")
                break
            soup = BeautifulSoup(response.content, "html.parser")

            for heading in soup.find_all(["h2", "h3", "h4"]):
                anchor = heading.find("a", href=True)
                if not anchor:
                    continue
                title = clean_text(anchor.get_text(" ", strip=True))
                if not title or title.lower() in {"engagement opportunities", "on this page"}:
                    continue
                container = heading.find_parent(["article", "li", "div"]) or heading.parent
                if not container:
                    continue
                text = clean_text(container.get_text(" ", strip=True))
                if "engagement" not in text.lower() and "consult" not in text.lower():
                    continue
                item_url = absolute_url(response.url, anchor.get("href"))
                canonical = item_url
                if canonical in seen_items:
                    continue
                seen_items.add(canonical)
                match = OPEN_UNTIL_RE.search(text)
                date_text = clean_text(match.group(1)) if match else ""
                end_date = parse_date(date_text)
                end_at = (
                    combine_local_date(end_date, source.get("default_time", "23:59"), timezone_name)
                    if end_date
                    else None
                )
                lifecycle = "open" if "is open" in text.lower() or "ongoing" in text.lower() else "closed"
                if (
                    lifecycle == "closed"
                    and end_at
                    and end_at < now - timedelta(days=int(source.get("lookback_days", 14)))
                ):
                    continue
                description = text.replace(title, "", 1).strip()
                combined = f"{title} {description}"
                events.append(
                    build_event(
                        source=source,
                        now=now,
                        canonical_key=canonical,
                        title=title,
                        source_url=item_url,
                        event_type="consultation",
                        lifecycle=lifecycle,
                        description=description[:1500] or None,
                        end_at=end_at,
                        all_day=True,
                        confidence="confirmed" if end_date else "expected",
                        topics=infer_topics(combined, source.get("topic_rules")),
                        raw={"date_text": date_text},
                    )
                )

            next_anchor = soup.find("a", string=re.compile(r"Next", re.IGNORECASE))
            next_url = (
                absolute_url(response.url, next_anchor.get("href")) if next_anchor and next_anchor.get("href") else None
            )

        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=last_status,
            warnings=warnings,
        )
