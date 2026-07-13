from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import absolute_url, clean_text, combine_local_date, parse_date
from .base import CollectResult
from .common import build_event

BC_REG_RE = re.compile(r"\b\d{1,4}/\d{4}\b")
EFFECTIVE_RE = re.compile(
    r"effective(?:\s+on)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)


class BCLawsRegulationsCollector:
    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        url = source["url"].format(year=now.year)
        source = dict(source)
        source["url"] = url
        result = client.get(url, conditional_key=f"{source['id']}:{now.year}")
        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        soup = BeautifulSoup(result.content, "html.parser")
        events = []
        seen: set[str] = set()
        timezone_name = source.get("timezone", "America/Vancouver")
        earliest = now - timedelta(days=int(source.get("lookback_days", 30)))
        latest = now + timedelta(days=int(source.get("lookahead_days", 180)))

        for row in soup.find_all("tr"):
            values = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            row_text = clean_text(" ".join(values))
            reg_matches = BC_REG_RE.findall(row_text)
            if not reg_matches:
                continue
            deposit_date = None
            for value in reversed(values):
                parsed = parse_date(value)
                if parsed and 1990 <= parsed.year <= now.year + 2:
                    deposit_date = parsed
                    break
            if not deposit_date:
                continue
            published_at = combine_local_date(deposit_date, "12:00", timezone_name)
            effective_match = EFFECTIVE_RE.search(row_text)
            effective_date = (
                parse_date(effective_match.group(0).replace("effective", "", 1)) if effective_match else None
            )
            start_at = combine_local_date(effective_date, "00:00", timezone_name) if effective_date else None
            if published_at < earliest and not (start_at and now <= start_at <= latest):
                continue

            reg_number = reg_matches[-1]
            canonical = f"bc-regulation|{reg_number}"
            if canonical in seen:
                continue
            seen.add(canonical)
            anchor = row.find("a", href=True)
            item_url = absolute_url(result.url, anchor.get("href") if anchor else None)
            title = row_text[:500]
            events.append(
                build_event(
                    source=source,
                    now=now,
                    canonical_key=canonical,
                    title=title,
                    source_url=item_url,
                    event_type="final_regulation",
                    lifecycle="published",
                    description=row_text,
                    start_at=start_at,
                    published_at=published_at,
                    all_day=True,
                    topics=infer_topics(row_text, source.get("topic_rules")),
                    identifiers={"bc_regulation": reg_number},
                    raw={
                        "deposit_date": deposit_date.isoformat(),
                        "effective_date": effective_date.isoformat() if effective_date else None,
                    },
                )
            )

        warnings = []
        if not events:
            warnings.append("No recent or forthcoming B.C. regulation rows were parsed.")
        return CollectResult(
            source_id=source["id"],
            events=events,
            status="partial" if warnings else "ok",
            http_status=result.status_code,
            warnings=warnings,
        )
