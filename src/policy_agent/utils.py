from __future__ import annotations

import calendar
import hashlib
import json
import re
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

WHITESPACE_RE = re.compile(r"\s+")
DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b",
    re.IGNORECASE,
)
QUARTER_RE = re.compile(
    r"\b(?:Q([1-4])|([1-4])(?:st|nd|rd|th)?\s+quarter|quarter\s+([1-4]))(?:\s+of)?\s*,?\s*(20\d{2})\b",
    re.IGNORECASE,
)
YEAR_ONLY_RE = re.compile(r"^\s*(20\d{2})\s*$")


def parse_period(value: str | None) -> tuple[date, date] | None:
    """Parse an exact date, month, quarter, or year into an inclusive date range."""
    if not value:
        return None
    text = clean_text(value)

    exact = extract_first_date(text)
    if exact:
        return exact, exact

    quarter = QUARTER_RE.search(text)
    if quarter:
        number = int(next(group for group in quarter.groups()[:3] if group))
        year = int(quarter.group(4))
        start_month = 1 + (number - 1) * 3
        end_month = start_month + 2
        return date(year, start_month, 1), date(year, end_month, calendar.monthrange(year, end_month)[1])

    month_year = MONTH_YEAR_RE.search(text)
    if month_year:
        month = date_parser.parse(month_year.group(1)).month
        year = int(month_year.group(2))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    year_only = YEAR_ONLY_RE.match(text)
    if year_only:
        year = int(year_only.group(1))
        return date(year, 1, 1), date(year, 12, 31)
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime, assumed_timezone: str = "UTC") -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(assumed_timezone))
    return value


def to_utc(value: datetime, assumed_timezone: str = "UTC") -> datetime:
    return ensure_aware(value, assumed_timezone).astimezone(timezone.utc)


def combine_local_date(date_value: date, hhmm: str, timezone_name: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":", 1))
    return datetime.combine(date_value, time(hour, minute), ZoneInfo(timezone_name)).astimezone(timezone.utc)


def parse_date(value: str | None, *, dayfirst: bool = False) -> date | None:
    if not value:
        return None
    text = clean_text(value)
    try:
        return date_parser.parse(text, dayfirst=dayfirst, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None


def parse_datetime(
    value: str | None,
    *,
    assumed_timezone: str = "UTC",
    dayfirst: bool = False,
) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(clean_text(value), dayfirst=dayfirst, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    return to_utc(parsed, assumed_timezone)


def extract_first_date(text: str) -> date | None:
    for pattern in (ISO_DATE_RE, DATE_RE):
        match = pattern.search(text)
        if match:
            parsed = parse_date(match.group(0))
            if parsed:
                return parsed
    return None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return WHITESPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()


def normalize_key(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def stable_id(*parts: Any, length: int = 20) -> str:
    normalized = "|".join(clean_text(str(part)).lower() for part in parts if part is not None)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length]


def content_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def absolute_url(base: str, href: str | None) -> str:
    if not href:
        return base
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(serialized)
        handle.flush()
    temp_path.replace(path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = clean_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
