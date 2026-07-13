from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pdfplumber
from bs4 import BeautifulSoup, Tag

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, normalize_key
from .base import CollectResult
from .common import build_event
from .policy_signal_utils import FULL_DATE_RE, extract_dates

REPORT_LINK_RE = re.compile(
    r"/DocumentViewer/(?:en|fr)/(\d+)-(\d+)/([A-Z]{3,5})/report-(\d+)(?:/|$)",
    re.IGNORECASE,
)
COMMITTEE_LINK_RE = re.compile(r"/Committees/(?:en|fr)/([A-Z]{3,5})(?:/|$)", re.IGNORECASE)
REPORT_TITLE_RE = re.compile(r"^Report\s+(\d+)\s*(?:-|–|—|:)\s*(.+)$", re.IGNORECASE)
RESPONSE_REQUEST_MARKERS = (
    "pursuant to standing order 109",
    "request that the government table a comprehensive response",
    "request a government response",
    "government response is requested",
)
DEFAULT_PRIORITY_COMMITTEES = (
    "FINA",
    "ENVI",
    "TRAN",
    "RNNR",
    "INDU",
    "PACP",
    "SECU",
    "HUMA",
    "INAN",
    "OGGO",
    "CIIT",
    "HESA",
    "FAAE",
)
DEFAULT_PRIORITY_KEYWORDS = (
    "budget",
    "fiscal",
    "tax",
    "tariff",
    "trade",
    "housing",
    "infrastructure",
    "energy",
    "electricity",
    "climate",
    "environment",
    "natural resources",
    "competition",
    "industrial",
    "technology",
    "immigration",
    "health",
    "public safety",
    "indigenous",
    "transport",
    "regulation",
    "economic",
    "cost of living",
    "affordability",
)


@dataclass(slots=True)
class ReportRecord:
    key: str
    report_url: str
    event_url: str
    committee: str
    report_number: str
    title: str
    event_kind: str
    event_date: date
    raw_text: str


def _report_container(anchor: Tag) -> Tag | None:
    current: Tag | None = anchor
    best: Tag | None = None
    for _ in range(9):
        if current is None:
            break
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        text = clean_text(parent.get_text(" ", strip=True))
        lower = text.lower()
        if len(text) <= 3200 and FULL_DATE_RE.search(text) and (
            "presented to the house" in lower or "government response" in lower
        ):
            best = parent
            # The smallest matching row/card is generally the most accurate.
            if parent.name in {"tr", "li", "article"}:
                return parent
        if len(text) > 10000:
            break
        current = parent
    return best


def _committee_code(container: Tag, fallback: str) -> str:
    for anchor in container.find_all("a", href=True):
        href = str(anchor.get("href"))
        match = COMMITTEE_LINK_RE.search(href)
        if match:
            return match.group(1).upper()
        label = clean_text(anchor.get_text(" ", strip=True)).upper()
        if re.fullmatch(r"[A-Z]{3,5}", label):
            return label
    return fallback.upper()


def _report_title(container: Tag, report_url: str, report_number: str) -> str:
    candidates: list[str] = []
    for anchor in container.find_all("a", href=True):
        href = absolute_url(report_url, str(anchor.get("href")))
        if REPORT_LINK_RE.search(href):
            label = clean_text(anchor.get_text(" ", strip=True))
            if label:
                candidates.append(label)
    for label in sorted(candidates, key=len, reverse=True):
        match = REPORT_TITLE_RE.match(label)
        if match:
            return clean_text(match.group(2))
    return f"Committee Report {report_number}"


def _event_link(container: Tag, report_url: str, event_kind: str) -> str:
    if event_kind == "response":
        for anchor in container.find_all("a", href=True):
            if "government response" in clean_text(anchor.get_text(" ", strip=True)).lower():
                return absolute_url(report_url, str(anchor.get("href")))
    return report_url


def parse_report_listing(content: bytes, base_url: str) -> list[ReportRecord]:
    """Parse report-presentation and government-response cards from the House work page."""
    soup = BeautifulSoup(content, "html.parser")
    records: dict[tuple[str, str], ReportRecord] = {}

    for anchor in soup.find_all("a", href=REPORT_LINK_RE):
        href = absolute_url(base_url, str(anchor.get("href")))
        match = REPORT_LINK_RE.search(href)
        if not match:
            continue
        parliament, session, fallback_committee, report_number = match.groups()
        report_url = href.split("/response-", 1)[0].rstrip("/") + "/"
        container = _report_container(anchor)
        if container is None:
            continue
        text = clean_text(container.get_text(" ", strip=True))
        lower = text.lower()
        if "government response" in lower:
            event_kind = "response"
        elif "presented to the house" in lower:
            event_kind = "presented"
        else:
            continue
        dates = extract_dates(text)
        if not dates:
            continue
        event_date = dates[-1]
        committee = _committee_code(container, fallback_committee)
        title = _report_title(container, report_url, report_number)
        key = f"{parliament}-{session}|{committee}|{report_number}"
        record = ReportRecord(
            key=key,
            report_url=report_url,
            event_url=_event_link(container, report_url, event_kind),
            committee=committee,
            report_number=report_number,
            title=title,
            event_kind=event_kind,
            event_date=event_date,
            raw_text=text,
        )
        records[(key, event_kind)] = record

    return list(records.values())


def _pagination_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True)).lower()
        href = absolute_url(base_url, str(anchor.get("href")))
        if "show=reports" not in href.lower():
            continue
        if "pagenumber=" not in href.lower():
            continue
        if label in {"next", "›", "»"} or re.fullmatch(r"\d+", label):
            links.append(href)
    return list(dict.fromkeys(links))


def _response_requested_in_html(content: bytes) -> bool:
    soup = BeautifulSoup(content, "html.parser")
    text = clean_text((soup.find("main") or soup).get_text(" ", strip=True)).lower()
    return any(marker in text for marker in RESPONSE_REQUEST_MARKERS)


def _linked_report_pdf(soup: BeautifulSoup, base_url: str) -> str | None:
    for anchor in soup.find_all("a", href=True):
        href = absolute_url(base_url, str(anchor.get("href")))
        label = clean_text(anchor.get_text(" ", strip=True)).lower()
        if ".pdf" in href.lower() and (
            "report" in label or "printable" in label or "pdf" in label
        ):
            return href
    return None


def _response_requested_in_pdf(content: bytes) -> bool:
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = []
            # The Standing Order request is normally near the start or end.
            selected = list(pdf.pages[:3]) + list(pdf.pages[-3:])
            seen: set[int] = set()
            for page in selected:
                page_number = int(getattr(page, "page_number", 0))
                if page_number in seen:
                    continue
                seen.add(page_number)
                pages.append(page.extract_text() or "")
        text = clean_text(" ".join(pages)).lower()
        return any(marker in text for marker in RESPONSE_REQUEST_MARKERS)
    except Exception:
        return False


def _is_priority_report(record: ReportRecord, source: dict[str, Any]) -> bool:
    priority_committees = {
        str(value).upper()
        for value in source.get("priority_committees", DEFAULT_PRIORITY_COMMITTEES)
    }
    priority_keywords = tuple(
        str(value).lower()
        for value in source.get("priority_keywords", DEFAULT_PRIORITY_KEYWORDS)
    )
    title_lower = record.title.lower()
    if record.committee in priority_committees:
        if source.get("include_bill_reports", False):
            return True
        if re.match(r"^bill\s+[cs]-?\d+", title_lower):
            return any(keyword in title_lower for keyword in priority_keywords)
        return True
    return any(keyword in title_lower for keyword in priority_keywords)


class HouseCommitteeReportsCollector:
    """Track substantive committee reports, tabled responses, and confirmed 120-day response deadlines."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        lookback_days = int(source.get("lookback_days", 180))
        response_horizon_days = int(source.get("response_horizon_days", 240))
        earliest = (now - timedelta(days=lookback_days)).date()
        latest_response = (now + timedelta(days=response_horizon_days)).date()
        max_pages = int(source.get("max_pages", 10))
        max_report_pages = int(source.get("max_report_pages", 80))
        timezone_name = source.get("timezone", "America/Toronto")

        queue = [source["url"]]
        visited: set[str] = set()
        records: dict[tuple[str, str], ReportRecord] = {}
        warnings: list[str] = []
        failed = 0
        pages = 0

        while queue and pages < max_pages:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                result = client.get(page_url, allow_not_modified=False)
                pages += 1
                for record in parse_report_listing(result.content, str(result.url)):
                    if record.event_date >= earliest and _is_priority_report(record, source):
                        records[(record.key, record.event_kind)] = record
                soup = BeautifulSoup(result.content, "html.parser")
                for href in _pagination_links(soup, str(result.url)):
                    if href not in visited and href not in queue:
                        queue.append(href)
            except Exception as exc:
                failed += 1
                warnings.append(f"House report list failed ({page_url}): {type(exc).__name__}: {exc}")

        events = []
        responses_present = {
            key for (key, event_kind), _record in records.items() if event_kind == "response"
        }

        presented_records = [
            record for (_key, kind), record in records.items() if kind == "presented"
        ]
        response_records = [
            record for (_key, kind), record in records.items() if kind == "response"
        ]

        for record in response_records:
            published_at = combine_local_date(record.event_date, "12:00", timezone_name)
            combined = clean_text(f"{record.committee} {record.title} government response")
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=f"house-report-response|{record.key}|{record.event_date}",
                    title=f"Government response tabled: {record.committee} — {record.title}",
                    source_url=record.event_url,
                    event_type="policy_event",
                    lifecycle="published",
                    description=(
                        f"The Government of Canada tabled its response to {record.committee} "
                        f"Report {record.report_number}, “{record.title}”."
                    ),
                    published_at=published_at,
                    confidence="confirmed",
                    topics=infer_topics(combined, source.get("topic_rules")),
                    identifiers={
                        "committee": record.committee,
                        "report_number": record.report_number,
                        "report_key": record.key,
                    },
                    raw={
                        "event_kind": "government_response_tabled",
                        "event_date": str(record.event_date),
                        "report_url": record.report_url,
                        "high_profile": True,
                    },
                )
            )

        detail_fetches = 0
        for record in sorted(presented_records, key=lambda item: item.event_date, reverse=True):
            published_at = combine_local_date(record.event_date, "12:00", timezone_name)
            combined = clean_text(f"{record.committee} {record.title}")
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=f"house-report-presented|{record.key}|{record.event_date}",
                    title=f"Committee report presented: {record.committee} — {record.title}",
                    source_url=record.report_url,
                    event_type="policy_event",
                    lifecycle="published",
                    description=(
                        f"{record.committee} Report {record.report_number}, “{record.title}”, "
                        "was presented to the House of Commons."
                    ),
                    published_at=published_at,
                    confidence="confirmed",
                    topics=infer_topics(combined, source.get("topic_rules")),
                    identifiers={
                        "committee": record.committee,
                        "report_number": record.report_number,
                        "report_key": record.key,
                    },
                    raw={
                        "event_kind": "report_presented",
                        "event_date": str(record.event_date),
                        "high_profile": True,
                    },
                )
            )

            if record.key in responses_present or detail_fetches >= max_report_pages:
                continue

            requested = False
            try:
                detail = client.get(record.report_url, allow_not_modified=False)
                detail_fetches += 1
                requested = _response_requested_in_html(detail.content)
                if not requested and source.get("parse_report_pdfs", True):
                    soup = BeautifulSoup(detail.content, "html.parser")
                    pdf_url = _linked_report_pdf(soup, str(detail.url))
                    if pdf_url:
                        pdf = client.get(pdf_url, allow_not_modified=False)
                        requested = _response_requested_in_pdf(pdf.content)
            except Exception as exc:
                failed += 1
                warnings.append(
                    f"House report detail failed ({record.report_url}): {type(exc).__name__}: {exc}"
                )

            if not requested:
                continue

            due_date = record.event_date + timedelta(days=120)
            if due_date < now.date() - timedelta(days=lookback_days) or due_date > latest_response:
                continue
            due_at = combine_local_date(due_date, "17:00", timezone_name)
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=f"house-report-response-due|{record.key}|{due_date}",
                    title=f"Government response due: {record.committee} — {record.title}",
                    source_url=record.report_url,
                    event_type="filing_deadline",
                    lifecycle="due",
                    description=(
                        f"A government response to {record.committee} Report {record.report_number}, "
                        f"“{record.title}”, was requested under Standing Order 109 and is due "
                        "within 120 calendar days of presentation."
                    ),
                    start_at=due_at,
                    all_day=True,
                    confidence="confirmed",
                    topics=infer_topics(combined, source.get("topic_rules")),
                    identifiers={
                        "committee": record.committee,
                        "report_number": record.report_number,
                        "report_key": record.key,
                    },
                    raw={
                        "event_kind": "government_response_due",
                        "presentation_date": str(record.event_date),
                        "response_due_date": str(due_date),
                        "standing_order": "109",
                        "high_profile": True,
                    },
                )
            )

        unique = {event.id: event for event in events}
        if pages and not records:
            warnings.append("No recent priority committee report events were parsed.")
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
