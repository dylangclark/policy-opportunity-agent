from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

from ..classify import infer_event_type, infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, normalize_key, parse_date, parse_period
from .base import CollectResult
from .common import build_event

EXHIBIT_RE = re.compile(r"\b(?:[A-Z]{1,4}|IR)[-\s]?\d+(?:[-.]\d+)*\b", re.IGNORECASE)
APPLICATION_ID_RE = re.compile(r"applicationid=(\d+)", re.IGNORECASE)


def _cells(row: Tag) -> list[Tag]:
    return list(row.find_all(["th", "td"], recursive=False)) or list(row.find_all(["th", "td"]))


def _cell_texts(row: Tag) -> list[str]:
    return [clean_text(cell.get_text(" ", strip=True)) for cell in _cells(row)]


def _find_header_map(table: Tag) -> dict[str, int]:
    for row in table.find_all("tr"):
        values = _cell_texts(row)
        lowered = [value.lower() for value in values]
        if any("date" in value for value in lowered) or any("entity" in value for value in lowered):
            return {value.lower(): index for index, value in enumerate(values) if value}
    return {}


def _pick(values: list[str], header_map: dict[str, int], candidates: Iterable[str]) -> str:
    for candidate in candidates:
        for header, index in header_map.items():
            if candidate in header and index < len(values):
                return values[index]
    return ""


class BCUCDeadlinesCollector:
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
        events = []
        timezone_name = source.get("timezone", "America/Vancouver")
        seen: set[str] = set()

        for table in soup.find_all("table"):
            header_map = _find_header_map(table)
            for row in table.find_all("tr"):
                values = _cell_texts(row)
                if len(values) < 2:
                    continue
                due_text = _pick(values, header_map, ("due date", "deadline", "date"))
                due_date = parse_date(due_text)
                if due_date is None:
                    # Fallback for accessible tables whose headers are not in the same element.
                    due_index = next(
                        (i for i, value in enumerate(values) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)), None
                    )
                    if due_index is None:
                        continue
                    due_date = parse_date(values[due_index])
                    due_text = values[due_index]
                if due_date is None:
                    continue

                entity = _pick(values, header_map, ("entity",))
                proceeding = _pick(values, header_map, ("proceeding", "application"))
                item = _pick(values, header_map, ("regulatory item", "item", "subject"))
                order = _pick(values, header_map, ("order", "letter"))

                # Positional fallback matches the current five-column BCUC table.
                if len(values) >= 5:
                    entity = entity or values[0]
                    proceeding = proceeding or values[1]
                    item = item or values[2]
                    order = order or values[4]
                elif len(values) >= 3:
                    item = item or values[-2]

                title_parts = [part for part in (entity, proceeding, item) if part]
                title = " — ".join(title_parts) or f"BCUC filing deadline {due_text}"
                row_link = row.find("a", href=True)
                item_url = absolute_url(result.url, row_link.get("href") if row_link else None)
                canonical = f"bcuc-deadline|{entity}|{proceeding}|{item}|{order}"
                if canonical in seen:
                    continue
                seen.add(canonical)
                combined = f"{title} {order}"
                event_type = infer_event_type(combined, "filing_deadline")
                if event_type in {"regulatory_order", "regulatory_decision"}:
                    event_type = "filing_deadline"

                events.append(
                    build_event(
                        source=source,
                        now=now,
                        canonical_key=canonical,
                        title=title,
                        source_url=item_url,
                        event_type=event_type,
                        lifecycle="due",
                        description=f"Order or letter: {order}" if order else None,
                        start_at=combine_local_date(due_date, source.get("default_time", "17:00"), timezone_name),
                        all_day=True,
                        topics=infer_topics(combined, source.get("topic_rules")),
                        identifiers={"order_or_letter": order} if order else {},
                        raw={"entity": entity, "proceeding": proceeding, "regulatory_item": item},
                    )
                )

        warnings = []
        if not events:
            warnings.append("No deadline rows were parsed; the BCUC page structure may have changed.")
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )


class BCUCProceedingsCollector:
    """Collect public docket metadata. It never downloads linked docket documents."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        warnings: list[str] = []
        events = []
        proceeding_urls: dict[str, str] = {}

        try:
            listing = client.get(source["url"], allow_not_modified=False)
            soup = BeautifulSoup(listing.content, "html.parser")
            for anchor in soup.find_all("a", href=APPLICATION_ID_RE):
                href = str(anchor.get("href"))
                match = APPLICATION_ID_RE.search(href)
                if not match:
                    continue
                application_id = match.group(1)
                row = anchor.find_parent("tr")
                row_text = (
                    clean_text(row.get_text(" ", strip=True)) if row else clean_text(anchor.get_text(" ", strip=True))
                )
                if source.get("only_in_progress", True) and row_text and "in progress" not in row_text.lower():
                    continue
                proceeding_urls[application_id] = absolute_url(listing.url, href)
        except Exception as exc:  # individual source status reports the details
            listing = None
            warnings.append(f"Proceedings listing failed: {type(exc).__name__}: {exc}")

        for application_id in source.get("application_ids", []):
            application_id = str(application_id)
            proceeding_urls.setdefault(
                application_id,
                f"https://www.bcuc.com/OurWork/ViewProceeding?applicationid={application_id}",
            )

        max_proceedings = int(source.get("max_proceedings", 40))
        selected = list(proceeding_urls.items())[:max_proceedings]
        if len(proceeding_urls) > max_proceedings:
            warnings.append(f"Limited BCUC docket collection to {max_proceedings} proceedings.")

        failed = 0
        for application_id, proceeding_url in selected:
            try:
                response = client.get(proceeding_url, allow_not_modified=False)
                page_events, page_warnings = self._parse_proceeding(
                    source=source,
                    now=now,
                    application_id=application_id,
                    url=response.url,
                    content=response.content,
                )
                events.extend(page_events)
                warnings.extend(page_warnings)
            except Exception as exc:
                failed += 1
                warnings.append(f"Application {application_id} failed: {type(exc).__name__}: {exc}")

        status = "ok"
        if failed or warnings:
            status = "partial"
        if not selected and not events:
            status = "failed"
            warnings.append("No BCUC proceeding URLs were available from the listing or configuration.")

        # Deduplicate responsive-table duplicates.
        unique = {event.id: event for event in events}
        return CollectResult(
            source_id=source["id"],
            events=list(unique.values()),
            status=status,
            http_status=listing.status_code if listing else None,
            warnings=warnings,
            error="; ".join(warnings) if status == "failed" else None,
        )

    def _parse_proceeding(
        self,
        *,
        source: dict[str, Any],
        now: datetime,
        application_id: str,
        url: str,
        content: bytes,
    ) -> tuple[list, list[str]]:
        soup = BeautifulSoup(content, "html.parser")
        page_title = clean_text((soup.find("h1") or soup.title or soup).get_text(" ", strip=True))
        lookback_days = int(source.get("docket_lookback_days", 120))
        earliest = (now - timedelta(days=lookback_days)).date()
        timezone_name = source.get("timezone", "America/Vancouver")
        events = []
        warnings: list[str] = []

        for table in soup.find_all("table"):
            header_map = _find_header_map(table)
            headers = " ".join(header_map.keys())

            # Timetable rows: Subject | Date
            if "subject" in headers and "date" in headers and "title" not in headers:
                for row in table.find_all("tr"):
                    values = _cell_texts(row)
                    if len(values) < 2:
                        continue
                    subject = _pick(values, header_map, ("subject",)) or values[0]
                    date_text = _pick(values, header_map, ("date",)) or values[-1]
                    scheduled_date = parse_date(date_text)
                    if not scheduled_date or scheduled_date < earliest:
                        continue
                    combined = f"{page_title} {subject}"
                    canonical = f"bcuc-timetable|{application_id}|{subject}"
                    events.append(
                        build_event(
                            source=source,
                            now=now,
                            canonical_key=canonical,
                            title=f"{page_title} — {subject}",
                            source_url=url,
                            event_type=infer_event_type(combined, "regulatory_timetable"),
                            lifecycle="scheduled",
                            start_at=combine_local_date(
                                scheduled_date,
                                source.get("default_time", "09:00"),
                                timezone_name,
                            ),
                            all_day=True,
                            topics=infer_topics(combined, source.get("topic_rules")),
                            identifiers={"application_id": application_id},
                            raw={"subject": subject, "date_text": date_text},
                        )
                    )
                continue

            # Docket rows: Date | Title/Exhibit | Description
            for row in table.find_all("tr"):
                values = _cell_texts(row)
                if len(values) < 2:
                    continue
                date_index = None
                filing_date = None
                for index, value in enumerate(values):
                    parsed = parse_date(value)
                    if parsed and 1990 <= parsed.year <= now.year + 2:
                        date_index = index
                        filing_date = parsed
                        break
                if filing_date is None or filing_date < earliest:
                    continue

                exhibit_id = ""
                exhibit_index = None
                for index, value in enumerate(values):
                    match = EXHIBIT_RE.fullmatch(value.strip()) or EXHIBIT_RE.search(value)
                    if match and index != date_index:
                        exhibit_id = clean_text(match.group(0)).replace(" ", "-")
                        exhibit_index = index
                        break
                if not exhibit_id:
                    continue

                description_parts = [
                    value for index, value in enumerate(values) if index not in {date_index, exhibit_index} and value
                ]
                description = " — ".join(description_parts)
                row_text = clean_text(" ".join(values))
                confidential = "confidential" in row_text.lower()
                row_link = row.find("a", href=True)
                document_url = absolute_url(url, row_link.get("href") if row_link else None)
                source_url = url if confidential else document_url
                combined = f"{page_title} {exhibit_id} {description}"
                canonical = f"bcuc-docket|{application_id}|{exhibit_id}"
                events.append(
                    build_event(
                        source=source,
                        now=now,
                        canonical_key=canonical,
                        title=f"{page_title} — {exhibit_id}: {description or 'Docket filing'}",
                        source_url=source_url,
                        event_type=infer_event_type(combined, "evidence_filing"),
                        lifecycle="published",
                        description=description or None,
                        published_at=combine_local_date(filing_date, "12:00", timezone_name),
                        all_day=True,
                        topics=infer_topics(combined, source.get("topic_rules")),
                        identifiers={"application_id": application_id, "exhibit_id": exhibit_id},
                        metadata_only=confidential,
                        raw={
                            "application_id": application_id,
                            "exhibit_id": exhibit_id,
                            "confidential": confidential,
                            "document_link_observed": bool(row_link),
                        },
                    )
                )

        if not events:
            warnings.append(f"Application {application_id}: no recent timetable or docket rows parsed.")
        return events, warnings


class BCUCAnticipatedFilingsCollector:
    """Parse the public BCUC anticipated-filings table into expected filing windows."""

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
        timezone_name = source.get("timezone", "America/Vancouver")
        earliest = (now - timedelta(days=int(source.get("lookback_days", 30)))).date()
        latest = (now + timedelta(days=int(source.get("lookahead_days", 550)))).date()
        events = []
        seen: set[str] = set()

        for table in soup.find_all("table"):
            header_map = _find_header_map(table)
            for row in table.find_all("tr"):
                values = _cell_texts(row)
                if len(values) < 2:
                    continue
                timing = _pick(values, header_map, ("anticipated filing", "filing date", "timing", "date"))
                period = parse_period(timing) if timing else None
                timing_index = None
                if period is None:
                    for index, value in enumerate(values):
                        parsed = parse_period(value)
                        if parsed:
                            period = parsed
                            timing = value
                            timing_index = index
                            break
                else:
                    timing_index = next((index for index, value in enumerate(values) if value == timing), None)
                if period is None:
                    continue
                start_date, end_date = period
                if end_date < earliest or start_date > latest:
                    continue

                entity = _pick(values, header_map, ("regulated entity", "entity", "company", "utility"))
                filing = _pick(
                    values, header_map, ("anticipated application", "application", "filing", "description", "title")
                )
                non_timing = [value for index, value in enumerate(values) if index != timing_index and value]
                if not entity and non_timing:
                    entity = non_timing[0]
                if not filing:
                    filing = " — ".join(non_timing[1:] if len(non_timing) > 1 else non_timing)
                filing = filing or "Anticipated regulatory filing"
                title = " — ".join(part for part in (entity, filing) if part)
                canonical = f"bcuc-anticipated|{normalize_key(entity)}|{normalize_key(filing)}"
                if canonical in seen:
                    continue
                seen.add(canonical)
                anchor = row.find("a", href=True)
                item_url = absolute_url(result.url, anchor.get("href") if anchor else None)
                combined = f"{title} {timing}"
                events.append(
                    build_event(
                        source=source,
                        now=now,
                        canonical_key=canonical,
                        title=title[:500],
                        source_url=item_url,
                        event_type="regulatory_timetable",
                        lifecycle="scheduled",
                        description=f"Anticipated filing window: {timing}",
                        start_at=combine_local_date(start_date, source.get("default_time", "09:00"), timezone_name),
                        end_at=combine_local_date(end_date, source.get("end_time", "23:59"), timezone_name),
                        all_day=True,
                        confidence="expected",
                        topics=infer_topics(combined, source.get("topic_rules")),
                        identifiers={"anticipated_filing": filing},
                        raw={"entity": entity, "filing": filing, "timing_text": timing},
                    )
                )

        warnings = []
        if not events:
            warnings.append("No anticipated-filing rows were parsed; the BCUC page structure may have changed.")
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )
