from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree as ET
from urllib.parse import urldefrag

from bs4 import BeautifulSoup, Tag
import pdfplumber

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, parse_date, stable_id
from .base import CollectResult
from .common import build_event
from .policy_signal_utils import extract_dates

DOCUMENT_LINK_RE = re.compile(
    r"/civix/document/id/(?:complete/)?(?:oic|mo)/(?:oic_cur|mo|hmo)/[^?#]+",
    re.IGNORECASE,
)
ORDER_NUMBER_RE = re.compile(
    r"\b(ORDER IN COUNCIL|MINISTERIAL ORDER)\s*(?:NO\.?|NUMBER)?\s*([A-Z-]?\d{1,5})\b",
    re.IGNORECASE,
)
EFFECTIVE_RE = re.compile(
    r"(?:effective|comes? into force(?: on)?|commences? on|takes? effect(?: on)?)"
    r"[^.]{0,120}?"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+20\d{2}|20\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)
APPROVED_RE = re.compile(
    r"(?:Approved and Ordered|Approved|Ordered)\s*:?\s*"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+20\d{2}|20\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)
RESUME_RANGE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}\s*[–—-]\s*"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"
    r"(\d{1,2}),\s*(20\d{2})",
    re.IGNORECASE,
)

HIGH_SIGNAL_TERMS: dict[str, int] = {
    "comes into force": 5,
    "effective": 2,
    "regulation": 4,
    "regulations": 4,
    "amend": 3,
    "repeal": 3,
    "enact": 4,
    "special direction": 6,
    "ministerial direction": 6,
    "utilities commission": 5,
    "bc hydro": 5,
    "hydro and power authority": 5,
    "environmental assessment": 5,
    "climate": 4,
    "energy": 4,
    "electricity": 4,
    "natural gas": 4,
    "housing": 4,
    "tax": 4,
    "fee": 2,
    "tariff": 4,
    "transportation": 3,
    "transit": 4,
    "emergency": 4,
    "public health": 4,
    "mineral": 3,
    "mining": 3,
    "forestry": 3,
    "land use": 4,
    "local government": 3,
    "municipal": 3,
    "indigenous": 4,
    "first nation": 4,
    "funding": 2,
    "appropriation": 4,
    "borrowing": 4,
    "loan": 2,
    "certificate": 3,
    "exemption": 2,
    "prohibition": 3,
    "housing target order": 6,
    "designate": 1,
}

ROUTINE_TERMS: dict[str, int] = {
    "appointed as a member": -5,
    "reappointed": -5,
    "appointment of": -4,
    "remuneration": -3,
    "public service act": -2,
    "transferred and conveyed": -3,
    "land transferred": -2,
    "administrative tribunal": -3,
}

BOILERPLATE = {
    "full text pdf",
    "image",
    "copy cancel",
    "permanent link to page",
    "favourites",
    "back to top",
}



def _document_soup(content: bytes) -> BeautifulSoup:
    """Convert either an HTML document or a PDF into parseable text."""
    if content.lstrip().startswith(b"%PDF"):
        pages: list[str] = []

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                value = page.extract_text() or ""
                if value:
                    pages.append(value)

        # BeautifulSoup will preserve the extracted line structure in <pre>.
        escaped = (
            "\n".join(pages)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return BeautifulSoup(
            f"<html><body><pre>{escaped}</pre></body></html>",
            "html.parser",
        )

    return BeautifulSoup(content, "html.parser")


def _content_records(content: bytes) -> list[dict[str, str]]:
    """Read directory and document records from the CIVIX Content API."""
    root = ET.fromstring(content)
    records: list[dict[str, str]] = []

    for element in root:
        values = {
            child.tag.split("}")[-1]: clean_text(child.text or "")
            for child in list(element)
        }

        document_id = values.get("CIVIX_DOCUMENT_ID", "")
        document_type = values.get("CIVIX_DOCUMENT_TYPE", "")

        if not document_id or not document_type:
            continue

        records.append(
            {
                "id": document_id,
                "type": document_type.lower(),
                "title": values.get("CIVIX_DOCUMENT_TITLE", ""),
                "index": values.get("CIVIX_INDEX_ID", ""),
                "extension": values.get("CIVIX_DOCUMENT_EXT", "").lower(),
                "visible": values.get("CIVIX_DOCUMENT_VISIBLE", "true").lower(),
            }
        )

    return records


def _document_number(document_id: str) -> int:
    match = re.search(r"(\d{1,5})_\d{4}$", document_id, re.IGNORECASE)
    return int(match.group(1)) if match else -1


def _civix_document_url(
    base_url: str,
    aspect: str,
    index_id: str,
    document_id: str,
) -> str:
    return (
        f"{base_url.rstrip('/')}/civix/document/id/"
        f"{aspect}/{index_id}/{document_id}"
    )


def _discover_content_api_documents(
    *,
    source: dict[str, Any],
    client: HttpClient,
    now: datetime,
    warnings: list[str],
) -> list[tuple[str, str]]:
    """Traverse current-year CIVIX directories and return recent documents."""
    base_url = str(source.get("civix_base_url", "https://www.bclaws.gov.bc.ca"))
    lookback_days = int(source.get("lookback_days", 75))
    earliest_year = (now - timedelta(days=lookback_days)).year
    years = set(range(earliest_year, now.year + 1))

    collections = (
        {
            "kind": "oic",
            "aspect": "oic",
            "index": "oic_cur",
        },
        {
            "kind": "ministerial",
            "aspect": "mo",
            "index": "mo",
        },
    )

    discovered: list[tuple[str, str]] = []

    for collection in collections:
        aspect = collection["aspect"]
        index_id = collection["index"]
        kind = collection["kind"]
        root_url = (
            f"{base_url.rstrip('/')}/civix/content/"
            f"{aspect}/{index_id}/"
        )

        try:
            root_result = client.get(root_url, allow_not_modified=False)
            year_records = _content_records(root_result.content)
        except Exception as exc:
            warnings.append(
                f"BC Laws Content API root failed ({root_url}): "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        selected_years = [
            record
            for record in year_records
            if record["type"] == "dir"
            and record["title"].isdigit()
            and int(record["title"]) in years
            and record["visible"] != "false"
        ]

        for year_record in selected_years:
            year_url = f"{root_url}{year_record['id']}/"

            try:
                year_result = client.get(year_url, allow_not_modified=False)
                year_children = _content_records(year_result.content)
            except Exception as exc:
                warnings.append(
                    f"BC Laws Content API year failed ({year_url}): "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            if kind == "ministerial":
                documents = [
                    record
                    for record in year_children
                    if record["type"] == "document"
                    and record["visible"] != "false"
                ]

                documents.sort(
                    key=lambda record: _document_number(record["id"]),
                    reverse=True,
                )

                limit = int(source.get("max_ministerial_documents", 100))
                for record in documents[:limit]:
                    discovered.append(
                        (
                            _civix_document_url(
                                base_url,
                                aspect,
                                index_id,
                                record["id"],
                            ),
                            kind,
                        )
                    )
                continue

            # OIC documents are grouped into Resumes and numbered directories.
            child_directories = [
                record
                for record in year_children
                if record["type"] == "dir"
                and record["visible"] != "false"
            ]

            prefer_resumes = bool(source.get("prefer_resume_documents", True))
            if prefer_resumes:
                resume_dirs = [
                    record
                    for record in child_directories
                    if "resume" in record["title"].lower()
                ]
                if resume_dirs:
                    child_directories = resume_dirs

            for child_record in child_directories:
                child_url = f"{root_url}{child_record['id']}/"

                try:
                    child_result = client.get(
                        child_url,
                        allow_not_modified=False,
                    )
                    child_documents = _content_records(child_result.content)
                except Exception as exc:
                    warnings.append(
                        f"BC Laws Content API directory failed ({child_url}): "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue

                documents = [
                    record
                    for record in child_documents
                    if record["type"] == "document"
                    and record["visible"] != "false"
                ]

                documents.sort(
                    key=lambda record: _document_number(record["id"]),
                    reverse=True,
                )

                for record in documents:
                    discovered.append(
                        (
                            _civix_document_url(
                                base_url,
                                aspect,
                                index_id,
                                record["id"],
                            ),
                            kind,
                        )
                    )

    return discovered

def _field_map(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        key = cells[0].rstrip(":").lower()
        value = clean_text(" ".join(cells[1:]))
        if key and value:
            fields[key] = value
    return fields


def _label_value(lines: list[str], labels: tuple[str, ...]) -> str | None:
    for index, line in enumerate(lines):
        lower = line.lower().rstrip(":")
        for label in labels:
            if lower == label and index + 1 < len(lines):
                return lines[index + 1]
            if lower.startswith(label + ":"):
                value = clean_text(line.split(":", 1)[1])
                if value:
                    return value
    return None


def _policy_signal_score(text: str) -> int:
    lower = text.lower()
    score = sum(weight for term, weight in HIGH_SIGNAL_TERMS.items() if term in lower)
    score += sum(weight for term, weight in ROUTINE_TERMS.items() if term in lower)
    return score


def _event_type(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ("regulation", "comes into force", "repeal", "amend")):
        return "final_regulation"
    if any(term in lower for term in ("direction", "order in council", "ministerial order")):
        return "regulatory_order"
    return "policy_event"


def _summary_from_document(soup: BeautifulSoup, fields: dict[str, str]) -> str:
    for key in ("summary", "description", "order", "subject"):
        if fields.get(key):
            return fields[key]

    meta = soup.find("meta", attrs={"name": re.compile(r"description", re.IGNORECASE)})
    if isinstance(meta, Tag) and meta.get("content"):
        value = clean_text(str(meta.get("content")))
        if value and "bc laws" not in value.lower():
            return value

    candidates: list[str] = []
    for element in soup.find_all(["p", "li"]):
        value = clean_text(element.get_text(" ", strip=True))
        lower = value.lower()
        if len(value) < 25 or lower in BOILERPLATE:
            continue
        if any(term in lower for term in HIGH_SIGNAL_TERMS) or value.startswith(("The ", "A ", "An ")):
            candidates.append(value)
    return max(candidates, key=len) if candidates else ""


def _resume_publication_date(text: str) -> date | None:
    match = RESUME_RANGE_RE.search(text[:1800])
    if match:
        month_name = clean_text(match.group(1) or "")
        if not month_name:
            # Use the first month in the range.
            first_month = re.search(
                r"(January|February|March|April|May|June|July|August|September|October|November|December)",
                match.group(0),
                re.IGNORECASE,
            )
            month_name = first_month.group(1) if first_month else ""
        parsed = parse_date(f"{month_name} {match.group(2)}, {match.group(3)}")
        if parsed:
            return parsed
    dates = extract_dates(text[:2400])
    return max(dates) if dates else None


def _clean_summary_lines(lines: list[str]) -> str:
    excluded_prefixes = (
        "ministry responsible:",
        "statutory authority:",
        "approved and ordered",
        "order in council",
        "ministerial order",
    )
    values = [
        line
        for line in lines
        if line
        and line.lower() not in BOILERPLATE
        and not line.lower().startswith(excluded_prefixes)
        and not re.fullmatch(r"\d+", line)
    ]
    return clean_text(" ".join(values))


def _extract_resume_records(content: bytes, url: str) -> list[dict[str, Any]]:
    soup = _document_soup(content)
    raw_lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    lines = [line for line in raw_lines if line and line.lower() not in BOILERPLATE]
    page_text = "\n".join(lines)
    if len(ORDER_NUMBER_RE.findall(page_text)) < 2:
        return []

    resume_date = _resume_publication_date(page_text)
    approved_date: date | None = None
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for index, line in enumerate(lines):
        approved_match = APPROVED_RE.search(line)
        if approved_match:
            approved_date = parse_date(approved_match.group(1))
            continue
        if line.lower() in {"approved and ordered", "approved", "ordered"} and index + 1 < len(lines):
            candidate = parse_date(lines[index + 1])
            if candidate:
                approved_date = candidate
            continue

        order_match = ORDER_NUMBER_RE.search(line)
        if order_match:
            if current:
                blocks.append(current)
            current = {
                "kind": "oic" if "council" in order_match.group(1).lower() else "ministerial",
                "number": order_match.group(2).upper(),
                "approved_date": approved_date,
                "lines": [],
            }
            continue
        if current:
            current["lines"].append(line)
    if current:
        blocks.append(current)

    records: list[dict[str, Any]] = []
    for block in blocks:
        block_lines = list(block["lines"])
        ministry = _label_value(block_lines, ("ministry responsible", "ministry"))
        authority = _label_value(block_lines, ("statutory authority", "authority"))
        summary = _clean_summary_lines(block_lines)
        if not summary:
            continue
        effective_match = EFFECTIVE_RE.search(summary)
        effective_date = parse_date(effective_match.group(1)) if effective_match else None
        publication_date = block["approved_date"] or resume_date
        signal_text = clean_text(f"{ministry or ''} {authority or ''} {summary}")
        records.append(
            {
                "kind": block["kind"],
                "number": block["number"],
                "approved_date": publication_date,
                "effective_date": effective_date,
                "ministry": ministry,
                "authority": authority,
                "summary": summary,
                "signal_score": _policy_signal_score(signal_text),
                "document_url": url,
                "source_format": "weekly_resume",
            }
        )
    return records


def _extract_single_record(
    *,
    content: bytes,
    url: str,
    default_kind: str,
) -> dict[str, Any] | None:
    soup = _document_soup(content)
    fields = _field_map(soup)
    raw_lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    lines = [line for line in raw_lines if line and line.lower() not in BOILERPLATE]
    text = "\n".join(lines)

    kind = default_kind
    number = ""
    match = ORDER_NUMBER_RE.search(text)
    if match:
        kind = "oic" if "council" in match.group(1).lower() else "ministerial"
        number = match.group(2).upper()
    if not number:
        number = (
            fields.get("order in council")
            or fields.get("ministerial order")
            or fields.get("order number")
            or fields.get("number")
            or ""
        )
    if not number:
        href_tail = url.rstrip("/").rsplit("/", 1)[-1]
        number_match = re.search(r"([A-Z-]?\d{1,5})", href_tail, re.IGNORECASE)
        number = number_match.group(1).upper() if number_match else stable_id(url, length=10)

    approved_text = (
        fields.get("approved and ordered")
        or fields.get("approved")
        or fields.get("date")
        or _label_value(lines, ("approved and ordered", "approved", "date"))
        or ""
    )
    approved_match = APPROVED_RE.search(text)
    approved_date = parse_date(approved_match.group(1) if approved_match else approved_text)
    if approved_date is None:
        dates = extract_dates(text[:2500])
        approved_date = dates[0] if dates else None

    ministry = (
        fields.get("ministry responsible")
        or fields.get("ministry")
        or _label_value(lines, ("ministry responsible", "ministry"))
    )
    authority = (
        fields.get("statutory authority")
        or fields.get("authority")
        or _label_value(lines, ("statutory authority", "authority"))
    )
    summary = _summary_from_document(soup, fields)
    if not summary:
        summary = _clean_summary_lines(lines)

    if not summary:
        return None

    if len(summary) > 4000:
        summary = summary[:4000].rstrip()

    effective_match = EFFECTIVE_RE.search(text)
    effective_date = parse_date(effective_match.group(1)) if effective_match else None
    signal_text = clean_text(f"{ministry or ''} {authority or ''} {summary}")

    return {
        "kind": kind,
        "number": clean_text(number),
        "approved_date": approved_date,
        "effective_date": effective_date,
        "ministry": ministry,
        "authority": authority,
        "summary": summary,
        "signal_score": _policy_signal_score(signal_text),
        "document_url": url,
        "source_format": "individual_order",
    }


def _extract_records(content: bytes, url: str, default_kind: str) -> list[dict[str, Any]]:
    resume_records = _extract_resume_records(content, url)
    if resume_records:
        return resume_records
    single = _extract_single_record(content=content, url=url, default_kind=default_kind)
    return [single] if single else []


class BCOrdersCollector:
    """Collect high-signal B.C. Orders in Council and Ministerial Orders."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        browse_urls = source.get("browse_urls") or [source["url"]]
        document_urls: list[tuple[str, str]] = []
        warnings: list[str] = []
        failed = 0

        for browse_index, browse_url in enumerate(browse_urls):
            try:
                result = client.get(
                    str(browse_url),
                    conditional_key=f"{source['id']}:browse:{browse_index}",
                    allow_not_modified=False,
                )
                soup = BeautifulSoup(result.content, "html.parser")
                default_kind = "ministerial" if "/mo/" in str(result.url).lower() else "oic"
                for anchor in soup.find_all("a", href=True):
                    href = absolute_url(str(result.url), str(anchor.get("href")))
                    if DOCUMENT_LINK_RE.search(href):
                        document_urls.append((href, default_kind))
            except Exception as exc:
                failed += 1
                warnings.append(f"BC Laws browse page failed: {type(exc).__name__}: {exc}")

        # Use the CIVIX Content API when browse pages do not expose records.
        if not document_urls:
            document_urls.extend(
                _discover_content_api_documents(
                    source=source,
                    client=client,
                    now=now,
                    warnings=warnings,
                )
            )

        # Legacy HTML fallback for any collection not represented by the API.
        if not document_urls:
            for browse_url in browse_urls:
                try:
                    result = client.get(str(browse_url), allow_not_modified=False)
                    soup = BeautifulSoup(result.content, "html.parser")
                    child_urls: list[str] = []
                    parent_url, _ = urldefrag(str(result.url))

                    for anchor in soup.find_all("a", href=True):
                        raw_href = str(anchor.get("href", "")).strip()

                        if (
                            not raw_href
                            or raw_href.startswith("#")
                            or raw_href.lower().startswith(
                                ("javascript:", "mailto:")
                            )
                        ):
                            continue

                        href = absolute_url(
                            str(result.url),
                            raw_href,
                        )
                        href, _ = urldefrag(href)
                        lower_href = href.lower()

                        is_current_oic = (
                            "/civix/content/oic/oic_cur/" in lower_href
                        )
                        is_current_mo = (
                            "/civix/content/mo/mo/" in lower_href
                        )

                        if (
                            href != parent_url
                            and (is_current_oic or is_current_mo)
                        ):
                            child_urls.append(href)

                    child_urls = list(dict.fromkeys(child_urls))

                    for child_url in child_urls[:6]:
                        child = client.get(child_url, allow_not_modified=False)
                        child_soup = BeautifulSoup(child.content, "html.parser")
                        default_kind = "ministerial" if "/mo/" in child_url.lower() else "oic"
                        for anchor in child_soup.find_all("a", href=True):
                            href = absolute_url(str(child.url), str(anchor.get("href")))
                            if DOCUMENT_LINK_RE.search(href):
                                document_urls.append((href, default_kind))
                except Exception as exc:
                    warnings.append(f"BC Laws child browse failed: {type(exc).__name__}: {exc}")

        unique_documents: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for url, kind in document_urls:
            if url not in seen_urls:
                seen_urls.add(url)
                unique_documents.append((url, kind))

        if source.get("prefer_resume_documents", True):
            resume_documents = [
                item for item in unique_documents if re.search(r"resume\d+", item[0], re.IGNORECASE)
            ]
            if resume_documents:
                unique_documents = resume_documents

        max_documents = int(source.get("max_documents", 80))
        minimum_score = int(source.get("minimum_signal_score", 2))
        lookback_days = int(source.get("lookback_days", 75))
        lookahead_days = int(source.get("lookahead_days", 550))
        earliest = (now - timedelta(days=lookback_days)).date()
        latest = (now + timedelta(days=lookahead_days)).date()
        timezone_name = source.get("timezone", "America/Vancouver")
        events = []

        seen_orders: set[tuple[str, str, str]] = set()
        for document_url, default_kind in unique_documents[:max_documents]:
            try:
                document = client.get(document_url, allow_not_modified=False)
                for parsed in _extract_records(
                    content=document.content,
                    url=str(document.url),
                    default_kind=default_kind,
                ):
                    order_key = (
                        str(parsed["kind"]),
                        str(parsed["number"]),
                        str(parsed["approved_date"] or ""),
                    )
                    if order_key in seen_orders:
                        continue
                    seen_orders.add(order_key)
                    if parsed["signal_score"] < minimum_score:
                        continue
                    approved_date = parsed["approved_date"]
                    effective_date = parsed["effective_date"]
                    if approved_date is None and effective_date is None:
                        continue
                    if approved_date and approved_date < earliest and not (
                        effective_date and now.date() <= effective_date <= latest
                    ):
                        continue
                    if approved_date and approved_date > latest:
                        continue

                    kind_label = "Order in Council" if parsed["kind"] == "oic" else "Ministerial Order"
                    title_summary = str(parsed["summary"])
                    if len(title_summary) > 180:
                        title_summary = title_summary[:177].rstrip() + "..."
                    title = f"B.C. {kind_label} {parsed['number']}: {title_summary}"
                    combined = clean_text(
                        f"{title} {parsed['ministry'] or ''} {parsed['authority'] or ''} {parsed['summary']}"
                    )
                    start_at = (
                        combine_local_date(effective_date, "00:00", timezone_name)
                        if effective_date and effective_date >= now.date()
                        else None
                    )
                    published_at = (
                        combine_local_date(approved_date, "12:00", timezone_name) if approved_date else None
                    )
                    canonical = f"bc-legal-order|{parsed['kind']}|{parsed['number']}|{approved_date or ''}"
                    events.append(
                        build_event(
                            source=source,
                            now=now,
                            canonical_key=canonical,
                            title=title,
                            source_url=str(parsed.get("document_url") or document.url),
                            event_type=_event_type(combined),
                            lifecycle="scheduled" if start_at else "published",
                            description=str(parsed["summary"]),
                            start_at=start_at,
                            published_at=published_at,
                            all_day=bool(start_at),
                            confidence="confirmed",
                            topics=infer_topics(combined, source.get("topic_rules")),
                            identifiers={
                                "order_kind": str(parsed["kind"]),
                                "order_number": str(parsed["number"]),
                            },
                            raw={
                                "ministry": parsed["ministry"],
                                "statutory_authority": parsed["authority"],
                                "approved_date": str(approved_date) if approved_date else None,
                                "effective_date": str(effective_date) if effective_date else None,
                                "policy_signal_score": parsed["signal_score"],
                                "source_format": parsed["source_format"],
                                "high_profile": parsed["signal_score"] >= 7,
                            },
                        )
                    )
            except Exception as exc:
                failed += 1
                warnings.append(f"BC Laws document failed ({document_url}): {type(exc).__name__}: {exc}")

        if not unique_documents:
            warnings.append("No B.C. order document links were discovered.")
        if unique_documents and not events:
            warnings.append("No B.C. orders met the configured policy-signal threshold and date window.")

        status = "partial" if failed or not unique_documents else "ok"
        return CollectResult(
            source_id=source["id"],
            events=events,
            status=status,
            http_status=200 if unique_documents else None,
            warnings=warnings,
        )
