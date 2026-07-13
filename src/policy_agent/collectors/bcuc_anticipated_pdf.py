from __future__ import annotations

import calendar
import hashlib
import io
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pdfplumber
from bs4 import BeautifulSoup

from ..classify import infer_topics
from ..http import HttpClient
from .base import CollectResult
from .common import build_event


BCUC_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "*/*;q=0.8"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://www.bcuc.com/",
}

QUARTERS = [
    ("Q1", 376.75, 430.15, 1, 3),
    ("Q2", 430.03, 483.45, 4, 6),
    ("Q3", 483.34, 536.74, 7, 9),
    ("Q4", 536.62, 590.04, 10, 12),
]

COMPANY_X0 = 18.0
COMPANY_X1 = 84.5
TITLE_X0 = 84.5
TITLE_X1 = 376.6


def _words_in_box(
    page: Any,
    x0: float,
    top: float,
    x1: float,
    bottom: float,
) -> str:
    words = page.extract_words(
        x_tolerance=2,
        y_tolerance=2,
        keep_blank_chars=False,
        use_text_flow=False,
    )

    selected = [
        word
        for word in words
        if word["x0"] >= x0 - 1
        and word["x1"] <= x1 + 1
        and word["top"] >= top - 1
        and word["bottom"] <= bottom + 1
    ]

    selected.sort(key=lambda word: (round(word["top"], 1), word["x0"]))

    lines: list[list[Any]] = []

    for word in selected:
        if not lines or abs(lines[-1][0] - word["top"]) > 2.5:
            lines.append([word["top"], [word["text"]]])
        else:
            lines[-1][1].append(word["text"])

    return " ".join(
        " ".join(parts).strip()
        for _, parts in lines
        if parts
    ).strip()


def _quarter_range(rect: dict[str, Any]) -> dict[str, Any] | None:
    covered = []

    for name, qx0, qx1, start_month, end_month in QUARTERS:
        overlap = max(
            0,
            min(rect["x1"], qx1) - max(rect["x0"], qx0),
        )

        if overlap >= 10:
            covered.append((name, start_month, end_month))

    if not covered:
        return None

    return {
        "quarter_start": covered[0][0],
        "quarter_end": covered[-1][0],
        "start_month": covered[0][1],
        "end_month": covered[-1][2],
    }


def _fill_value(rect: dict[str, Any]) -> float:
    fill = rect.get("non_stroking_color")

    if isinstance(fill, (list, tuple)) and fill:
        return float(sum(fill) / len(fill))

    if isinstance(fill, (int, float)):
        return float(fill)

    return 0.0


def _stable_key(*parts: str) -> str:
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


class BCUCAnticipatedPDFCollector:
    def collect(
        self,
        source: dict[str, Any],
        client: HttpClient,
        now: datetime,
    ) -> CollectResult:
        page_result = client.get(
            source["url"],
            conditional_key=source["id"],
            headers=BCUC_BROWSER_HEADERS,
        )

        if page_result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=page_result.status_code,
                not_modified=True,
            )

        soup = BeautifulSoup(page_result.content, "html.parser")

        pdf_link = None
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])

            if (
                "anticipated" in href.lower()
                and href.lower().endswith(".pdf")
            ):
                pdf_link = urljoin(str(page_result.url), href)
                break

        if not pdf_link:
            return CollectResult(
                source_id=source["id"],
                status="partial",
                http_status=page_result.status_code,
                warnings=[
                    "The anticipated-filings PDF link was not found."
                ],
            )

        pdf_headers = {
            **BCUC_BROWSER_HEADERS,
            "Accept": (
                "application/pdf,application/octet-stream;q=0.9,"
                "*/*;q=0.8"
            ),
            "Referer": source["url"],
        }

        pdf_result = client.get(
            pdf_link,
            headers=pdf_headers,
            allow_not_modified=False,
        )

        content_type = str(
            pdf_result.headers.get("content-type", "")
        ).lower()

        if (
            "pdf" not in content_type
            and not pdf_result.content.startswith(b"%PDF")
        ):
            return CollectResult(
                source_id=source["id"],
                status="partial",
                http_status=pdf_result.status_code,
                warnings=[
                    "The anticipated-filings document was not returned "
                    "as a PDF."
                ],
            )

        events = []
        warnings = []

        try:
            with pdfplumber.open(io.BytesIO(pdf_result.content)) as pdf:
                page = pdf.pages[0]
                page_text = page.extract_text() or ""

                year_match = re.search(
                    r"Anticipated Major Regulatory Filings\s+"
                    r"(20\d{2})",
                    page_text,
                    re.IGNORECASE,
                )

                if not year_match:
                    raise ValueError(
                        "Could not determine the timetable year."
                    )

                year = int(year_match.group(1))

                updated_match = re.search(
                    r"Updated:\s*([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
                    page_text,
                )
                document_updated = (
                    updated_match.group(1)
                    if updated_match
                    else None
                )

                rectangles = sorted(
                    page.rects,
                    key=lambda rect: (
                        round(rect["top"], 2),
                        round(rect["x0"], 2),
                    ),
                )

                for rect in rectangles:
                    width = rect["x1"] - rect["x0"]
                    height = rect["bottom"] - rect["top"]

                    if not (
                        rect["top"] >= 120
                        and rect["bottom"] <= 580
                        and rect["x0"] >= 376
                        and width >= 45
                        and height >= 7
                    ):
                        continue

                    timing = _quarter_range(rect)
                    if timing is None:
                        continue

                    company = _words_in_box(
                        page,
                        COMPANY_X0,
                        rect["top"],
                        COMPANY_X1,
                        rect["bottom"],
                    )
                    filing = _words_in_box(
                        page,
                        TITLE_X0,
                        rect["top"],
                        TITLE_X1,
                        rect["bottom"],
                    )

                    if not company or not filing:
                        warnings.append(
                            "Skipped a timetable row with missing "
                            "company or filing text."
                        )
                        continue

                    timing_confidence = (
                        "likely"
                        if _fill_value(rect) < 0.3
                        else "uncertain"
                    )

                    start_month = timing["start_month"]
                    end_month = timing["end_month"]

                    timezone_name = source.get(
                        "timezone",
                        "America/Vancouver",
                    )
                    tz = ZoneInfo(timezone_name)

                    start_at = datetime(
                        year,
                        start_month,
                        1,
                        tzinfo=tz,
                    )

                    last_day = calendar.monthrange(
                        year,
                        end_month,
                    )[1]

                    end_at = datetime(
                        year,
                        end_month,
                        last_day,
                        23,
                        59,
                        59,
                        tzinfo=tz,
                    )

                    canonical_key = (
                        "bcuc-anticipated|"
                        + _stable_key(
                            company,
                            filing,
                            str(year),
                            timing["quarter_start"],
                            timing["quarter_end"],
                        )
                    )

                    description = (
                        f"{company} anticipates filing "
                        f"“{filing}” with the BCUC during "
                        f"{timing['quarter_start']}"
                    )

                    if timing["quarter_end"] != timing["quarter_start"]:
                        description += (
                            f"–{timing['quarter_end']}"
                        )

                    description += (
                        f" {year}. Timing is identified by the BCUC "
                        f"as {timing_confidence}."
                    )

                    events.append(
                        build_event(
                            source=source,
                            now=now,
                            canonical_key=canonical_key,
                            title=f"{company}: {filing}",
                            source_url=pdf_link,
                            event_type=source.get(
                                "default_event_type",
                                "regulatory_filing",
                            ),
                            lifecycle=source.get(
                                "lifecycle",
                                "anticipated",
                            ),
                            description=description,
                            start_at=start_at,
                            end_at=end_at,
                            all_day=True,
                            confidence=source.get(
                                "confidence",
                                "confirmed",
                            ),
                            topics=infer_topics(
                                f"{company} {filing}",
                                source.get("topic_rules"),
                            ),
                            identifiers={
                                "company": company,
                                "filing": filing,
                            },
                            raw={
                                "quarter_start": timing[
                                    "quarter_start"
                                ],
                                "quarter_end": timing[
                                    "quarter_end"
                                ],
                                "timing_confidence": (
                                    timing_confidence
                                ),
                                "document_updated": (
                                    document_updated
                                ),
                                "pdf_url": pdf_link,
                            },
                        )
                    )

        except Exception as exc:
            return CollectResult(
                source_id=source["id"],
                status="partial",
                http_status=pdf_result.status_code,
                warnings=[
                    f"Could not parse anticipated-filings PDF: {exc}"
                ],
            )

        if not events:
            warnings.append(
                "No anticipated filings were parsed from the PDF."
            )

        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=pdf_result.status_code,
            warnings=warnings,
        )
