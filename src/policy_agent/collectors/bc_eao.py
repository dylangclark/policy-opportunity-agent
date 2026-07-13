from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, normalize_key, parse_date
from .base import CollectResult
from .common import build_event
from .policy_signal_utils import FULL_DATE_RE, deadline_from_text

DEFAULT_INCLUDE_KEYWORDS = (
    "assessment complete",
    "referred to ministers",
    "referred to decision-makers",
    "certificate granted",
    "certificate issued",
    "certificate amendment granted",
    "certificate amendment approved",
    "environmental assessment certificate",
    "project approved",
    "project not approved",
    "decision issued",
    "decision released",
    "public comment period",
    "comment period",
    "draft assessment report",
    "draft environmental assessment certificate",
    "application accepted",
    "application received",
    "detailed project description received",
    "initial project description received",
    "readiness decision",
    "process order",
    "substantially started",
    "extension granted",
    "assessment suspended",
    "assessment resumed",
    "reviewable project designation",
)

DEFAULT_EXCLUDE_KEYWORDS = (
    "inspection record posted",
    "inspection report posted",
    "compliance self-report",
    "administrative penalty",
    "notice of non-compliance",
    "enforcement action",
    "enforcement order",
)

ACTION_TEXT = (
    "project info",
    "view document",
    "view engagement",
    "read more",
)


def _card_for_date(node: NavigableString | Tag) -> Tag | None:
    current = node.parent if isinstance(node, NavigableString) else node
    if not isinstance(current, Tag):
        return None
    for _ in range(10):
        text = clean_text(current.get_text(" ", strip=True))
        lower = text.lower()
        if 80 <= len(text) <= 6000 and any(action in lower for action in ACTION_TEXT):
            return current
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        current = parent
    return None


def _headings(card: Tag) -> list[str]:
    values: list[str] = []
    for element in card.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        text = clean_text(element.get_text(" ", strip=True))
        if not text or text.lower() in ACTION_TEXT:
            continue
        if text not in values:
            values.append(text)
    return values


def _project_and_headline(card: Tag, text: str) -> tuple[str, str]:
    headings = _headings(card)
    if len(headings) >= 2:
        return headings[0], headings[1]
    if len(headings) == 1:
        heading = headings[0]
        remainder = clean_text(text.replace(heading, "", 1))
        for action in ACTION_TEXT:
            remainder = remainder.replace(action.title(), "")
        date_match = FULL_DATE_RE.search(remainder)
        if date_match:
            remainder = clean_text(remainder[: date_match.start()])
        return heading, remainder[:220] or "EAO project update"

    # React-rendered cards sometimes expose project/headline as anchor text rather than headings.
    anchors = [
        clean_text(anchor.get_text(" ", strip=True))
        for anchor in card.find_all("a")
        if clean_text(anchor.get_text(" ", strip=True)).lower() not in ACTION_TEXT
    ]
    anchors = [value for value in anchors if value]
    if len(anchors) >= 2:
        return anchors[0], anchors[1]
    if anchors:
        return anchors[0], "EAO project update"
    return "B.C. environmental assessment project", text[:180]


def _source_link(card: Tag, base_url: str) -> str:
    preferred = None
    fallback = None
    for anchor in card.find_all("a", href=True):
        href = absolute_url(base_url, str(anchor.get("href")))
        label = clean_text(anchor.get_text(" ", strip=True)).lower()
        if "/p/" in href:
            fallback = fallback or href
        if label in {"read more", "view engagement", "view document(s)", "view documents"}:
            preferred = href
            break
    return preferred or fallback or base_url


def _event_type(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ("certificate granted", "certificate issued", "project approved", "decision issued")):
        return "regulatory_decision"
    if any(term in lower for term in ("public comment", "comment period", "view engagement")):
        return "consultation"
    if any(term in lower for term in ("referred to ministers", "assessment complete", "application accepted")):
        return "regulatory_timetable"
    if any(term in lower for term in ("process order", "extension granted", "readiness decision")):
        return "regulatory_order"
    return "policy_event"


def _high_profile(text: str) -> bool:
    lower = text.lower()
    return any(
        term in lower
        for term in (
            "referred to ministers",
            "referred to decision-makers",
            "certificate granted",
            "certificate issued",
            "project approved",
            "project not approved",
            "decision issued",
            "draft assessment report",
        )
    )


class BCEAOMilestonesCollector:
    """Collect high-signal project milestones from B.C.'s EAO EPIC activities page."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        include_keywords = tuple(
            str(value).lower() for value in source.get("include_keywords", DEFAULT_INCLUDE_KEYWORDS)
        )
        exclude_keywords = tuple(
            str(value).lower() for value in source.get("exclude_keywords", DEFAULT_EXCLUDE_KEYWORDS)
        )
        lookback_days = int(source.get("lookback_days", 75))
        lookahead_days = int(source.get("lookahead_days", 180))
        earliest = (now - timedelta(days=lookback_days)).date()
        latest = (now + timedelta(days=lookahead_days)).date()
        timezone_name = source.get("timezone", "America/Vancouver")
        max_pages = int(source.get("max_pages", 8))

        queue = [source["url"]]
        visited: set[str] = set()
        events = []
        warnings: list[str] = []
        failed = 0
        pages = 0
        seen_cards: set[str] = set()

        while queue and pages < max_pages:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                result = client.get(page_url, allow_not_modified=False)
                pages += 1
                soup = BeautifulSoup(result.content, "html.parser")

                for date_node in soup.find_all(string=FULL_DATE_RE):
                    date_match = FULL_DATE_RE.search(str(date_node))
                    published_date = parse_date(date_match.group(0) if date_match else None)
                    if not published_date or not (earliest <= published_date <= latest):
                        continue
                    card = _card_for_date(date_node)
                    if card is None:
                        continue
                    card_text = clean_text(card.get_text(" ", strip=True))
                    lower = card_text.lower()
                    if not any(keyword in lower for keyword in include_keywords):
                        continue
                    if any(keyword in lower for keyword in exclude_keywords) and not _high_profile(card_text):
                        continue
                    project, headline = _project_and_headline(card, card_text)
                    source_url = _source_link(card, str(result.url))
                    key = normalize_key(f"{project}|{headline}|{published_date}|{source_url}")
                    if not key or key in seen_cards:
                        continue
                    seen_cards.add(key)

                    deadline = deadline_from_text(card_text, timezone_name)
                    event_type = _event_type(card_text)
                    start_at = deadline if event_type == "consultation" and deadline else None
                    lifecycle = "due" if start_at else "published"
                    published_at = combine_local_date(published_date, "12:00", timezone_name)
                    title = f"{project}: {headline}"
                    combined = clean_text(f"{title} {card_text}")
                    events.append(
                        build_event(
                            source=source,
                            now=now,
                            canonical_key=f"bc-eao|{key}",
                            title=title,
                            source_url=source_url,
                            event_type=event_type,
                            lifecycle=lifecycle,
                            description=card_text[:3500],
                            start_at=start_at,
                            published_at=published_at,
                            all_day=bool(start_at),
                            confidence="confirmed",
                            topics=infer_topics(combined, source.get("topic_rules")),
                            identifiers={"project": project},
                            raw={
                                "project": project,
                                "headline": headline,
                                "published_date": str(published_date),
                                "deadline": deadline.isoformat() if deadline else None,
                                "high_profile": _high_profile(card_text),
                            },
                        )
                    )

                for anchor in soup.find_all("a", href=True):
                    label = clean_text(anchor.get_text(" ", strip=True)).lower()
                    href = absolute_url(str(result.url), str(anchor.get("href")))
                    if label in {"next", "›", "»"} or re.fullmatch(r"\d+", label):
                        if href not in visited and "projects.eao.gov.bc.ca" in href:
                            queue.append(href)
            except Exception as exc:
                failed += 1
                warnings.append(f"EAO activities page failed ({page_url}): {type(exc).__name__}: {exc}")

        unique = {event.id: event for event in events}
        if pages and not unique:
            warnings.append("No high-signal EAO milestones were parsed within the configured lookback window.")
        status = "partial" if failed else "ok"
        if not pages:
            status = "failed"
        return CollectResult(
            source_id=source["id"],
            events=list(unique.values()),
            status=status,
            http_status=200 if pages else None,
            warnings=warnings,
            error="; ".join(warnings) if status == "failed" else None,
        )
