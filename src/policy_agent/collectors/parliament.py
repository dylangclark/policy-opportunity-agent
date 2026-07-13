from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, parse_date, stable_id
from .base import CollectResult
from .common import build_event

FULL_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?", re.IGNORECASE)
COMMITTEE_RE = re.compile(r"\b[A-Z]{3,5}\b")


class HouseCommitteeCollector:
    """Heuristic parser for the official House committee meeting list."""

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
        timezone_name = source.get("timezone", "America/Toronto")
        lookahead = now + timedelta(days=int(source.get("lookahead_days", 90)))
        events = []
        seen: set[str] = set()

        for node in soup.find_all(string=FULL_DATE_RE):
            date_match = FULL_DATE_RE.search(clean_text(str(node)))
            if not date_match:
                continue
            meeting_date = parse_date(date_match.group(0))
            if not meeting_date:
                continue
            start_at = combine_local_date(meeting_date, source.get("default_time", "09:00"), timezone_name)
            if start_at < now - timedelta(days=2) or start_at > lookahead:
                continue

            container: Tag | None = node.parent if isinstance(node.parent, Tag) else None
            chosen: Tag | None = None
            for _ in range(5):
                if container is None:
                    break
                text = clean_text(container.get_text(" ", strip=True))
                if 40 <= len(text) <= 2500 and (TIME_RE.search(text) or COMMITTEE_RE.search(text)):
                    chosen = container
                    break
                container = container.parent if isinstance(container.parent, Tag) else None
            if chosen is None:
                continue
            text = clean_text(chosen.get_text(" ", strip=True))
            code_match = COMMITTEE_RE.search(text)
            code = code_match.group(0) if code_match else "COMMITTEE"
            headings = chosen.find_all(["h2", "h3", "h4", "strong"])
            heading_text = next(
                (
                    clean_text(item.get_text(" ", strip=True))
                    for item in headings
                    if clean_text(item.get_text(" ", strip=True))
                ),
                "",
            )
            title = heading_text or f"House committee meeting — {code}"
            if date_match.group(0) not in title:
                title = f"{title} — {date_match.group(0)}"
            anchor = chosen.find("a", href=True)
            item_url = absolute_url(result.url, anchor.get("href") if anchor else None)
            canonical = f"house-committee|{code}|{meeting_date.isoformat()}|{stable_id(text)}"
            if canonical in seen:
                continue
            seen.add(canonical)
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=item_url,
                    event_type="committee_meeting",
                    lifecycle="scheduled",
                    description=text[:2000],
                    start_at=start_at,
                    all_day=False,
                    topics=infer_topics(text, source.get("topic_rules")),
                    identifiers={"committee_code": code},
                )
            )

        return CollectResult(
            source_id=source["id"],
            events=events,
            status="ok",
            http_status=result.status_code,
        )
