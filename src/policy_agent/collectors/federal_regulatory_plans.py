from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urldefrag

from bs4 import BeautifulSoup, Tag

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, normalize_key, stable_id
from .base import CollectResult
from .common import build_event
from .policy_signal_utils import heading_section_text, period_from_text, period_to_datetimes, sentence_chunks

MILESTONE_MARKERS = (
    "canada gazette",
    "part i",
    "part ii",
    "pre-publication",
    "prepublication",
    "final publication",
    "public consultation",
    "consultation",
    "planned publication",
    "anticipated publication",
    "expected publication",
    "target publication",
    "coming into force",
    "come into force",
    "expected to be",
    "planned for",
    "anticipated for",
    "in the first quarter",
    "in the second quarter",
    "in the third quarter",
    "in the fourth quarter",
)

SECTION_REQUIRED_TERMS = (
    "regulation",
    "regulations",
    "order",
    "regulatory initiative",
    "enabling act",
    "canada gazette",
    "consultation",
)

BOILERPLATE_HEADINGS = {
    "on this page",
    "about the forward regulatory plan",
    "forward regulatory plan",
    "regulatory initiatives",
    "for more information",
    "departmental contact information",
    "contact us",
    "related links",
    "previous plans",
    "updates",
    "purpose",
}

LOW_SIGNAL_TERMS = (
    "miscellaneous amendment",
    "housekeeping amendment",
    "technical amendment",
    "typographical",
    "administrative update",
)

GENERIC_INITIATIVE_TITLES = (
    "public consultation opportunities",
    "initiatives planned for",
    "planned aviation initiatives",
    "planned marine initiatives",
    "planned multimodal initiatives",
    "planned rail initiatives",
    "planned road initiatives",
    "health canada forward regulatory plan",
    "food and drugs act",
    "administrative leadership",
    "employer",
    "miscellaneous amendments to the weights and measures regulations",
)

DATE_OR_PERIOD_RE = re.compile(
    r"(?:20\d{2}|Q[1-4]|[1-4](?:st|nd|rd|th)?\s+quarter|"
    r"spring|summer|fall|autumn|winter|early|mid|late|"
    r"January|February|March|April|May|June|July|August|September|October|November|December)",
    re.IGNORECASE,
)


def _stage(text: str) -> tuple[str, str]:
    lower = text.lower()
    if "part ii" in lower or "final publication" in lower or "finalized" in lower:
        return "final_regulation", "Canada Gazette Part II/final publication"
    if "part i" in lower or "pre-publication" in lower or "prepublication" in lower:
        return "proposed_regulation", "Canada Gazette Part I/pre-publication"
    if "consult" in lower or "engagement" in lower:
        return "consultation", "consultation"
    if "coming into force" in lower or "come into force" in lower:
        return "final_regulation", "coming into force"
    return "regulatory_timetable", "regulatory milestone"


def _headings(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    sections: list[tuple[str, str, str]] = []

    for details in soup.find_all("details"):
        summary = details.find("summary")
        if not summary:
            continue
        title = clean_text(summary.get_text(" ", strip=True))
        body = clean_text(details.get_text(" ", strip=True))
        if title and body and title.lower() not in BOILERPLATE_HEADINGS:
            sections.append((title, body, "details"))

    for heading in soup.find_all(["h2", "h3", "h4"]):
        title = clean_text(heading.get_text(" ", strip=True))
        if not title or title.lower() in BOILERPLATE_HEADINGS:
            continue
        body = heading_section_text(heading)
        if len(body) < 40:
            continue
        sections.append((title, body, heading.name))

    return sections


def _table_sections(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    sections: list[tuple[str, str, str]] = []
    for table_index, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [clean_text(cell.get_text(" ", strip=True)).lower() for cell in rows[0].find_all(["th", "td"])]
        for row_index, row in enumerate(rows[1:], start=1):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            payload = {headers[index] if index < len(headers) else f"column_{index}": value for index, value in enumerate(cells)}
            title = ""
            for key, value in payload.items():
                if any(token in key for token in ("title", "initiative", "regulation")) and value:
                    title = value
                    break
            title = title or cells[0]
            body = clean_text(" ".join(f"{key}: {value}" for key, value in payload.items() if value))
            if title and body:
                sections.append((title, body, f"table-{table_index}-row-{row_index}"))
    return sections


def _milestones(text: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for chunk in sentence_chunks(text):
        lower = chunk.lower()
        if not DATE_OR_PERIOD_RE.search(chunk):
            continue
        if not any(marker in lower for marker in MILESTONE_MARKERS):
            continue
        event_type, stage = _stage(chunk)
        candidates.append((event_type, f"{stage}: {chunk}"))

    if candidates:
        return candidates

    # Some pages use table cells or paragraph fragments without punctuation.
    lower = text.lower()
    if DATE_OR_PERIOD_RE.search(text) and any(marker in lower for marker in MILESTONE_MARKERS):
        event_type, stage = _stage(text)
        candidates.append((event_type, f"{stage}: {text[:900]}"))
    return candidates


def _is_generic_initiative_title(title: str) -> bool:
    normalized_title = clean_text(title).lower()
    return any(
        normalized_title == term
        or normalized_title.startswith(term + " ")
        for term in GENERIC_INITIATIVE_TITLES
    )


def _looks_like_initiative(title: str, body: str) -> bool:
    if _is_generic_initiative_title(title):
        return False

    combined = f"{title} {body}".lower()
    return any(term in combined for term in SECTION_REQUIRED_TERMS)


def _is_low_signal(title: str, body: str, source: dict[str, Any]) -> bool:
    combined = f"{title} {body}".lower()
    if any(keyword.lower() in combined for keyword in source.get("always_include_keywords", [])):
        return False
    return any(term in combined for term in source.get("exclude_keywords", LOW_SIGNAL_TERMS))


def _is_relevant_policy_area(title: str, body: str, source: dict[str, Any]) -> bool:
    keywords = source.get("include_keywords", [])
    if not keywords:
        return True

    combined = f"{title} {body}".lower()
    return any(str(keyword).lower() in combined for keyword in keywords)


class FederalRegulatoryPlansCollector:
    """Extract dated publication, consultation, and coming-into-force milestones from federal plans."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        plan_pages = source.get("plan_pages") or [{"institution": source.get("institution"), "url": source["url"]}]
        timezone_name = source.get("timezone", "America/Toronto")
        lookback_days = int(source.get("lookback_days", 45))
        lookahead_days = int(source.get("lookahead_days", 730))
        earliest = (now - timedelta(days=lookback_days)).date()
        latest = (now + timedelta(days=lookahead_days)).date()
        events = []
        warnings: list[str] = []
        failed = 0
        parsed_pages = 0

        for page_index, page_config in enumerate(plan_pages):
            page_url = str(page_config["url"])
            department = clean_text(str(page_config.get("institution") or page_config.get("department") or source["institution"]))
            try:
                result = client.get(page_url, allow_not_modified=False)
                parsed_pages += 1
                soup = BeautifulSoup(result.content, "html.parser")
                sections = _table_sections(soup) + _headings(soup)
                seen_sections: set[str] = set()

                for title, body, structure in sections:
                    section_key = normalize_key(f"{title}|{body[:300]}")
                    if not section_key or section_key in seen_sections:
                        continue
                    seen_sections.add(section_key)
                    if (
                        not _looks_like_initiative(title, body)
                        or _is_low_signal(title, body, source)
                        or not _is_relevant_policy_area(title, body, source)
                    ):
                        continue

                    for event_type, milestone_text in _milestones(body):
                        period = period_from_text(milestone_text)
                        if not period:
                            continue
                        if period[1] < earliest or period[0] > latest:
                            continue
                        start_at, end_at = period_to_datetimes(period, timezone_name)
                        _, stage_label = _stage(milestone_text)
                        canonical = (
                            f"federal-reg-plan|{normalize_key(department)}|{normalize_key(title)}|"
                            f"{normalize_key(stage_label)}|{period[0]}|{period[1]}"
                        )
                        combined = clean_text(f"{department} {title} {body} {milestone_text}")
                        events.append(
                            build_event(
                                source=source,
                                now=now,
                                canonical_key=canonical,
                                title=f"{department}: {title} — {stage_label}",
                                source_url=str(result.url),
                                event_type=event_type,
                                lifecycle="scheduled",
                                description=milestone_text,
                                start_at=start_at,
                                end_at=end_at,
                                all_day=True,
                                confidence="expected",
                                topics=infer_topics(combined, source.get("topic_rules")),
                                identifiers={
                                    "department": department,
                                    "initiative": title,
                                    "milestone": stage_label,
                                },
                                raw={
                                    "department": department,
                                    "initiative_title": title,
                                    "milestone_text": milestone_text,
                                    "period_start": str(period[0]),
                                    "period_end": str(period[1]),
                                    "page_structure": structure,
                                    "plan_url": str(result.url),
                                },
                            )
                        )

                # Follow explicit initiative child pages when the index page contains only titles.
                if page_config.get("follow_child_pages"):
                    child_links: list[tuple[str, str]] = []
                    for anchor in soup.find_all("a", href=True):
                        href = absolute_url(
                            str(result.url),
                            str(anchor.get("href")),
                        )
                        href, fragment = urldefrag(href)
                        parent_url, _ = urldefrag(str(result.url))
                        label = clean_text(anchor.get_text(" ", strip=True))
                        lower_href = href.lower()

                        # Fragment-only anchors point to sections on the page
                        # already downloaded and must not trigger another request.
                        if (
                            "forward-regulatory" in lower_href
                            and href != parent_url
                            and label
                            and "previous" not in label.lower()
                        ):
                            child_links.append((href, label))
                    seen_children: set[str] = set()
                    for child_url, label in child_links[: int(page_config.get("max_child_pages", 30))]:
                        if child_url in seen_children:
                            continue
                        seen_children.add(child_url)
                        try:
                            child = client.get(child_url, allow_not_modified=False)
                            child_soup = BeautifulSoup(child.content, "html.parser")
                            child_body = clean_text((child_soup.find("main") or child_soup).get_text(" ", strip=True))
                            if (
                                _is_generic_initiative_title(label)
                                or _is_low_signal(label, child_body, source)
                                or not _is_relevant_policy_area(
                                    label,
                                    child_body,
                                    source,
                                )
                            ):
                                continue
                            for event_type, milestone_text in _milestones(child_body):
                                period = period_from_text(milestone_text)
                                if not period or period[1] < earliest or period[0] > latest:
                                    continue
                                start_at, end_at = period_to_datetimes(period, timezone_name)
                                _, stage_label = _stage(milestone_text)
                                canonical = (
                                    f"federal-reg-plan|{normalize_key(department)}|{normalize_key(label)}|"
                                    f"{normalize_key(stage_label)}|{period[0]}|{period[1]}"
                                )
                                combined = clean_text(f"{department} {label} {child_body} {milestone_text}")
                                events.append(
                                    build_event(
                                        source=source,
                                        now=now,
                                        canonical_key=canonical,
                                        title=f"{department}: {label} — {stage_label}",
                                        source_url=str(child.url),
                                        event_type=event_type,
                                        lifecycle="scheduled",
                                        description=milestone_text,
                                        start_at=start_at,
                                        end_at=end_at,
                                        all_day=True,
                                        confidence="expected",
                                        topics=infer_topics(combined, source.get("topic_rules")),
                                        identifiers={
                                            "department": department,
                                            "initiative": label,
                                            "milestone": stage_label,
                                        },
                                        raw={
                                            "department": department,
                                            "initiative_title": label,
                                            "milestone_text": milestone_text,
                                            "period_start": str(period[0]),
                                            "period_end": str(period[1]),
                                            "plan_url": str(child.url),
                                            "parent_plan_url": str(result.url),
                                        },
                                    )
                                )
                        except Exception as exc:
                            failed += 1
                            warnings.append(
                                f"Forward regulatory plan child page failed ({child_url}): {type(exc).__name__}: {exc}"
                            )
            except Exception as exc:
                failed += 1
                warnings.append(f"Forward regulatory plan failed ({page_url}): {type(exc).__name__}: {exc}")

        # Remove repeated initiative-stage records exposed through multiple
        # departmental pages. Preserve distinct consultation, proposed, and
        # final-publication milestones.
        deduplicated: dict[tuple[str, str, str], Any] = {}

        for event in events:
            key = (
                normalize_key(str(event.identifiers.get("department", ""))),
                normalize_key(str(event.identifiers.get("initiative", ""))),
                normalize_key(str(event.identifiers.get("milestone", ""))),
            )

            existing = deduplicated.get(key)

            if existing is None:
                deduplicated[key] = event
                continue

            existing_date = (
                existing.start_at
                or existing.published_at
                or existing.end_at
            )
            event_date = (
                event.start_at
                or event.published_at
                or event.end_at
            )

            # Prefer the earliest upcoming milestone. If both are historical,
            # retain the most recently dated observation.
            if existing_date is None:
                deduplicated[key] = event
            elif event_date is None:
                continue
            elif event_date >= now and (
                existing_date < now or event_date < existing_date
            ):
                deduplicated[key] = event
            elif event_date < now and existing_date < now and event_date > existing_date:
                deduplicated[key] = event

        unique = {event.id: event for event in deduplicated.values()}
        if parsed_pages and not unique:
            warnings.append("No dated federal regulatory milestones were parsed from the configured plan pages.")
        status = "partial" if failed else "ok"
        if not parsed_pages:
            status = "failed"
        return CollectResult(
            source_id=source["id"],
            events=list(unique.values()),
            status=status,
            http_status=200 if parsed_pages else None,
            warnings=warnings,
            error="; ".join(warnings) if status == "failed" else None,
        )
