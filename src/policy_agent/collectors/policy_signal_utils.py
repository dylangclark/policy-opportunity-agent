from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..utils import clean_text, combine_local_date, parse_date, parse_period

FULL_DATE_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{1,2},\s+20\d{2}\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{1,2},\s+20\d{2}\b|"
    r"\b20\d{2}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

MONTH_DAY_YEAR_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{1,2},?\s+20\d{2}\b",
    re.IGNORECASE,
)

SEASON_RE = re.compile(r"\b(spring|summer|fall|autumn|winter)\s+(20\d{2})\b", re.IGNORECASE)
EARLY_MID_LATE_RE = re.compile(r"\b(early|mid|late)(?:-|\s+)(20\d{2})\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def strip_url_fragment(url: str) -> str:
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))


def extract_dates(text: str) -> list[date]:
    values: list[date] = []
    seen: set[date] = set()
    for match in FULL_DATE_RE.finditer(text):
        parsed = parse_date(match.group(0))
        if parsed and parsed not in seen:
            seen.add(parsed)
            values.append(parsed)
    return values


def period_from_text(text: str) -> tuple[date, date] | None:
    """Return a practical inclusive period from exact, month, quarter, season, or year language."""
    direct = parse_period(text)
    if direct:
        return direct

    dates = extract_dates(text)
    if dates:
        return min(dates), max(dates)

    season = SEASON_RE.search(text)
    if season:
        label = season.group(1).lower()
        year = int(season.group(2))
        if label == "spring":
            return date(year, 3, 1), date(year, 5, 31)
        if label == "summer":
            return date(year, 6, 1), date(year, 8, 31)
        if label in {"fall", "autumn"}:
            return date(year, 9, 1), date(year, 11, 30)
        return date(year, 12, 1), date(year + 1, 2, calendar.monthrange(year + 1, 2)[1])

    rough = EARLY_MID_LATE_RE.search(text)
    if rough:
        label = rough.group(1).lower()
        year = int(rough.group(2))
        if label == "early":
            return date(year, 1, 1), date(year, 4, 30)
        if label == "mid":
            return date(year, 5, 1), date(year, 8, 31)
        return date(year, 9, 1), date(year, 12, 31)

    years = [int(value) for value in YEAR_RE.findall(text)]
    if years:
        return date(min(years), 1, 1), date(max(years), 12, 31)
    return None


def period_to_datetimes(
    period: tuple[date, date],
    timezone_name: str,
    *,
    start_time: str = "09:00",
    end_time: str = "17:00",
) -> tuple[datetime, datetime]:
    return (
        combine_local_date(period[0], start_time, timezone_name),
        combine_local_date(period[1], end_time, timezone_name),
    )


def deadline_from_text(text: str, timezone_name: str) -> datetime | None:
    lower = text.lower()
    trigger_positions = [
        lower.find(phrase)
        for phrase in (
            "comment period closes",
            "comments are due",
            "comments due",
            "deadline",
            "closes on",
            "open until",
            "accepted until",
            "submit by",
            "response due",
        )
        if lower.find(phrase) >= 0
    ]
    if not trigger_positions:
        return None
    start = min(trigger_positions)
    nearby = text[start : start + 280]
    dates = extract_dates(nearby)
    if not dates:
        return None
    return combine_local_date(dates[-1], "23:59", timezone_name)


def nearest_container(
    node: Tag,
    *,
    required_phrases: Iterable[str] = (),
    max_length: int = 6000,
    max_levels: int = 8,
) -> Tag:
    phrases = [phrase.lower() for phrase in required_phrases]
    current: Tag = node
    best = node
    for _ in range(max_levels):
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        text = clean_text(parent.get_text(" ", strip=True))
        if len(text) <= max_length and all(phrase in text.lower() for phrase in phrases):
            best = parent
        current = parent
    return best


def heading_section_text(heading: Tag) -> str:
    level = int(heading.name[1]) if heading.name and re.fullmatch(r"h[1-6]", heading.name) else 6
    parts: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and re.fullmatch(r"h[1-6]", sibling.name or ""):
            sibling_level = int(sibling.name[1])
            if sibling_level <= level:
                break
        if isinstance(sibling, Tag):
            text = clean_text(sibling.get_text(" ", strip=True))
        else:
            text = clean_text(str(sibling))
        if text:
            parts.append(text)
    return clean_text(" ".join(parts))


def visible_text(html: bytes | str) -> str:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()
    return clean_text(soup.get_text(" ", strip=True))


def within_window(
    value: date,
    now: datetime,
    *,
    lookback_days: int,
    lookahead_days: int,
) -> bool:
    current = now.date()
    return (current.toordinal() - lookback_days) <= value.toordinal() <= (
        current.toordinal() + lookahead_days
    )


def sentence_chunks(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+|\n+|\s*[;•]\s*", text)
    return [clean_text(chunk) for chunk in chunks if len(clean_text(chunk)) >= 20]


def dict_without_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
