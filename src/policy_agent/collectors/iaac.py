from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, normalize_key, parse_date
from .base import CollectResult
from .common import build_event
from .policy_signal_utils import FULL_DATE_RE, deadline_from_text, heading_section_text

PROJECT_LINK_RE = re.compile(r"/050/evaluations/proj/(\d+)", re.IGNORECASE)
REFERENCE_RE = re.compile(r"Reference Number:\s*(\d+)", re.IGNORECASE)
LAST_MODIFIED_RE = re.compile(r"Last Modified:\s*(20\d{2}-\d{2}-\d{2})", re.IGNORECASE)
ASSESSMENT_TYPE_RE = re.compile(r"Assessment Type:\s*(.+?)\s+Status:", re.IGNORECASE)
STATUS_RE = re.compile(r"Status:\s*(.+?)\s+Reference Number:", re.IGNORECASE)
LOCATION_RE = re.compile(r"Location\s*\((.+?)\)\s*Assessment Type:", re.IGNORECASE)

DEFAULT_EXCLUDED_TYPES = {
    "project on federal lands",
    "project outside of canada",
}

HIGH_VALUE_TYPES = (
    "planning phase",
    "impact assessment",
    "review panel",
    "regional assessment",
    "strategic assessment",
    "request for designation",
    "substitution",
    "environmental assessment",
)

MILESTONE_TERMS = (
    "notice of commencement",
    "public comment",
    "comments invited",
    "comment period",
    "decision statement",
    "amended decision statement",
    "determination",
    "time limit",
    "extension",
    "review panel",
    "panel appointed",
    "draft conditions",
    "draft assessment report",
    "impact statement guidelines",
    "tailored impact statement guidelines",
    "initial project description",
    "detailed project description",
    "impact statement accepted",
    "referred to",
    "minister's decision",
    "ministerial decision",
    "assessment commenced",
    "assessment terminated",
    "project designated",
)


def _result_container(anchor: Tag) -> Tag | None:
    current: Tag = anchor
    for _ in range(9):
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        text = clean_text(parent.get_text(" ", strip=True))
        if "Assessment Type:" in text and "Reference Number:" in text and len(text) <= 7000:
            return parent
        current = parent
    return None


def _metadata(text: str) -> dict[str, str | None]:
    reference = REFERENCE_RE.search(text)
    modified = LAST_MODIFIED_RE.search(text)
    assessment_type = ASSESSMENT_TYPE_RE.search(text)
    status = STATUS_RE.search(text)
    location = LOCATION_RE.search(text)
    return {
        "reference": reference.group(1) if reference else None,
        "last_modified": modified.group(1) if modified else None,
        "assessment_type": clean_text(assessment_type.group(1)) if assessment_type else None,
        "status": clean_text(status.group(1)) if status else None,
        "location": clean_text(location.group(1)) if location else None,
    }


def _latest_update(soup: BeautifulSoup) -> tuple[str, str | None]:
    for heading in soup.find_all(["h2", "h3", "h4"]):
        heading_text = clean_text(heading.get_text(" ", strip=True))
        if "latest update" not in heading_text.lower():
            continue
        body = heading_section_text(heading)
        link = None
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
                break
            if isinstance(sibling, Tag):
                anchor = sibling.find("a", href=True)
                if anchor:
                    link = str(anchor.get("href"))
                    break
        return body, link

    main_text = clean_text((soup.find("main") or soup).get_text(" ", strip=True))
    match = re.search(r"Latest update\s+(.{20,1800}?)(?:Project Details|Documents|Contact|Subscribe)", main_text, re.IGNORECASE)
    return (clean_text(match.group(1)), None) if match else ("", None)


def _milestone_type(text: str) -> tuple[str, str]:
    lower = text.lower()
    if any(term in lower for term in ("decision statement", "minister's decision", "ministerial decision", "determination")):
        return "regulatory_decision", "decision"
    if any(term in lower for term in ("public comment", "comments invited", "comment period")):
        return "consultation", "public consultation"
    if any(term in lower for term in ("extension", "time limit", "review panel", "panel appointed")):
        return "regulatory_order", "process order"
    return "regulatory_timetable", "assessment milestone"


def _headline(update_text: str, fallback: str) -> str:
    text = clean_text(update_text)
    if not text:
        return fallback
    date_match = FULL_DATE_RE.search(text)
    if date_match:
        text = clean_text(text[date_match.end() :]) or clean_text(text[: date_match.start()])
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return first_sentence[:220] if first_sentence else fallback


class IAACMilestonesCollector:
    """Collect recent high-impact project milestones from the federal Impact Assessment Registry."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        excluded_types = {
            str(value).lower() for value in source.get("exclude_assessment_types", DEFAULT_EXCLUDED_TYPES)
        }
        lookback_days = int(source.get("lookback_days", 60))
        earliest = (now - timedelta(days=lookback_days)).date()
        max_pages = int(source.get("max_pages", 6))
        max_projects = int(source.get("max_projects", 35))
        timezone_name = source.get("timezone", "America/Toronto")

        queue = [source["url"]]
        visited_pages: set[str] = set()
        candidates: list[dict[str, Any]] = []
        seen_projects: set[str] = set()
        warnings: list[str] = []
        failed = 0
        pages = 0

        while queue and pages < max_pages and len(candidates) < max_projects:
            page_url = queue.pop(0)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            try:
                result = client.get(page_url, allow_not_modified=False)
                pages += 1
                soup = BeautifulSoup(result.content, "html.parser")
                for anchor in soup.find_all("a", href=PROJECT_LINK_RE):
                    href = absolute_url(str(result.url), str(anchor.get("href")))
                    reference_match = PROJECT_LINK_RE.search(href)
                    if not reference_match or href in seen_projects:
                        continue
                    container = _result_container(anchor)
                    if container is None:
                        continue
                    text = clean_text(container.get_text(" ", strip=True))
                    meta = _metadata(text)
                    assessment_type = str(meta.get("assessment_type") or "")
                    if assessment_type.lower() in excluded_types:
                        continue
                    if not any(term in assessment_type.lower() for term in HIGH_VALUE_TYPES):
                        continue
                    modified = parse_date(str(meta.get("last_modified") or ""))
                    if not modified or modified < earliest:
                        continue
                    title = clean_text(anchor.get_text(" ", strip=True))
                    title = re.sub(r"^Project\s+", "", title, flags=re.IGNORECASE)
                    seen_projects.add(href)
                    candidates.append(
                        {
                            "url": href,
                            "title": title,
                            "summary": text,
                            **meta,
                        }
                    )
                    if len(candidates) >= max_projects:
                        break

                for anchor in soup.find_all("a", href=True):
                    label = clean_text(anchor.get_text(" ", strip=True)).lower()
                    href = absolute_url(str(result.url), str(anchor.get("href")))
                    if label in {"next", "›", "»"} or re.fullmatch(r"\d+", label):
                        if "/050/evaluations/exploration" in href and href not in visited_pages:
                            queue.append(href)
            except Exception as exc:
                failed += 1
                warnings.append(f"IAAC registry search failed ({page_url}): {type(exc).__name__}: {exc}")

        events = []
        for candidate in candidates:
            project_url = str(candidate["url"])
            try:
                result = client.get(project_url, allow_not_modified=False)
                soup = BeautifulSoup(result.content, "html.parser")
                project_title = clean_text((soup.find("h1") or soup.title or soup).get_text(" ", strip=True))
                project_title = project_title or str(candidate["title"])
                update_text, update_href = _latest_update(soup)
                update_lower = update_text.lower()
                update_dates = [parse_date(match.group(0)) for match in FULL_DATE_RE.finditer(update_text)]
                update_dates = [value for value in update_dates if value]
                update_date = update_dates[0] if update_dates else parse_date(str(candidate.get("last_modified") or ""))
                if not update_date or update_date < earliest:
                    continue

                matching_terms = [term for term in MILESTONE_TERMS if term in update_lower]
                if not matching_terms:
                    # A newly registered designated project is itself a useful early signal.
                    if "planning phase" not in str(candidate.get("assessment_type") or "").lower():
                        continue
                    update_text = clean_text(
                        f"New or materially updated project in the Registry. {candidate.get('summary') or ''}"
                    )
                    stage_label = "new planning-phase project"
                    event_type = "regulatory_timetable"
                else:
                    event_type, stage_label = _milestone_type(update_text)

                deadline = deadline_from_text(update_text, timezone_name)
                source_url = str(result.url)

                if update_href:
                    candidate_url = absolute_url(
                        str(result.url),
                        update_href,
                    )
                    scheme = urlparse(candidate_url).scheme.lower()

                    if scheme in {"http", "https"}:
                        source_url = candidate_url
                headline = _headline(update_text, stage_label)
                combined = clean_text(f"{project_title} {update_text} {candidate.get('assessment_type') or ''}")
                published_at = combine_local_date(update_date, "12:00", timezone_name)
                start_at = deadline if event_type == "consultation" and deadline else None
                canonical = (
                    f"iaac|{candidate.get('reference') or normalize_key(project_title)}|"
                    f"{update_date}|{normalize_key(stage_label)}|{normalize_key(headline[:120])}"
                )
                events.append(
                    build_event(
                        source=source,
                        now=now,
                        canonical_key=canonical,
                        title=f"{project_title}: {headline}",
                        source_url=source_url,
                        event_type=event_type,
                        lifecycle="due" if start_at else "published",
                        description=update_text[:4000],
                        start_at=start_at,
                        published_at=published_at,
                        all_day=bool(start_at),
                        confidence="confirmed",
                        topics=infer_topics(combined, source.get("topic_rules")),
                        identifiers={
                            "reference_number": str(candidate.get("reference") or ""),
                            "project": project_title,
                        },
                        raw={
                            "assessment_type": candidate.get("assessment_type"),
                            "registry_status": candidate.get("status"),
                            "location": candidate.get("location"),
                            "last_modified": candidate.get("last_modified"),
                            "update_date": str(update_date),
                            "stage": stage_label,
                            "high_profile": True,
                        },
                    )
                )
            except Exception as exc:
                failed += 1
                warnings.append(f"IAAC project page failed ({project_url}): {type(exc).__name__}: {exc}")

        unique = {event.id: event for event in events}
        if pages and not candidates:
            warnings.append("No recent high-impact IAAC projects were found in the parsed result pages.")
        if candidates and not unique:
            warnings.append("No high-signal IAAC project milestones were parsed from candidate project pages.")
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
