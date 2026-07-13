from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..classify import infer_event_type, infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, normalize_key, parse_date
from .base import CollectResult
from .common import build_event

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
FULL_DATE_RE = re.compile(
    rf"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday,?\s+)?(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,)?\s+20\d{{2}}\b",
    re.IGNORECASE,
)
MONTH_DAY_RE = re.compile(
    rf"\b(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _nearest_year(node: Tag, default_year: int) -> int:
    heading = node.find_previous(["h1", "h2", "h3", "h4", "h5"])
    if heading:
        match = YEAR_RE.search(clean_text(heading.get_text(" ", strip=True)))
        if match:
            return int(match.group(1))
    return default_year


def _extract_date(text: str, node: Tag, default_year: int) -> tuple[date | None, str]:
    for pattern in (ISO_DATE_RE, FULL_DATE_RE):
        match = pattern.search(text)
        if match:
            return parse_date(match.group(0)), match.group(0)
    match = MONTH_DAY_RE.search(text)
    if match:
        year = _nearest_year(node, default_year)
        return parse_date(f"{match.group(0)} {year}"), match.group(0)
    return None, ""


class HTMLDateListCollector:
    """Extract dated rows/list items from official calendar and deadline pages."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        result = client.get(source["url"], conditional_key=source["id"])
        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        soup = BeautifulSoup(result.content, "html.parser")
        for element in soup(["script", "style", "nav", "footer", "noscript"]):
            element.decompose()
        main = soup.find("main") or soup.find(id="main") or soup.body or soup
        tags = source.get("item_tags", ["tr", "li"])
        candidates = main.find_all(tags)
        timezone_name = source.get("timezone", "America/Toronto")
        earliest = now - timedelta(days=int(source.get("lookback_days", 14)))
        latest = now + timedelta(days=int(source.get("lookahead_days", 180)))
        includes = [str(value).lower() for value in source.get("include_keywords", [])]
        excludes = [str(value).lower() for value in source.get("exclude_keywords", [])]
        events = []
        seen: set[str] = set()

        for node in candidates:
            text = clean_text(node.get_text(" ", strip=True))
            if not (8 <= len(text) <= int(source.get("max_item_chars", 2000))):
                continue
            lowered = text.lower()
            if includes and not any(keyword in lowered for keyword in includes):
                continue
            if excludes and any(keyword in lowered for keyword in excludes):
                continue
            item_date, date_text = _extract_date(text, node, now.year)
            if item_date is None:
                continue
            start_at = combine_local_date(item_date, source.get("default_time", "09:00"), timezone_name)
            if start_at < earliest or start_at > latest:
                continue

            title = clean_text(text.replace(date_text, "", 1)).strip(" -–—:|,")
            if len(title) < 5:
                heading = node.find_previous(["h2", "h3", "h4", "h5"])
                heading_text = clean_text(heading.get_text(" ", strip=True)) if heading else ""
                title = heading_text or source.get("item_title_prefix") or source["name"]
            prefix = source.get("item_title_prefix")
            if prefix and not title.lower().startswith(str(prefix).lower()):
                title = f"{prefix} — {title}"

            anchor = node.find("a", href=True)
            item_url = absolute_url(result.url, anchor.get("href") if anchor else None)
            identity_text = normalize_key(title)
            if item_url != result.url:
                canonical = f"html-date|{source['id']}|{item_url}"
            else:
                canonical = f"html-date|{source['id']}|{item_date.year}|{identity_text}"
            if canonical in seen:
                continue
            seen.add(canonical)
            event_type = source.get("force_event_type") or infer_event_type(
                text,
                source.get("default_event_type", "policy_event"),
            )
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title[:500],
                    source_url=item_url,
                    event_type=event_type,
                    lifecycle=source.get("lifecycle", "scheduled"),
                    description=text[:2000],
                    start_at=start_at,
                    all_day=True,
                    confidence=source.get("confidence", "confirmed"),
                    topics=infer_topics(text, source.get("topic_rules")),
                    raw={"date_text": date_text},
                )
            )

        warnings = []
        if not events:
            warnings.append("No dated list items were parsed; the page may be empty or its structure may have changed.")
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )
