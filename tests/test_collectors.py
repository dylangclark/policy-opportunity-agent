from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from policy_agent.collectors.bc_laws import BCLawsRegulationsCollector
from policy_agent.collectors.bcuc import (
    BCUCAnticipatedFilingsCollector,
    BCUCDeadlinesCollector,
    BCUCProceedingsCollector,
)
from policy_agent.collectors.consultations import FederalConsultationsCollector
from policy_agent.collectors.html_date_list import HTMLDateListCollector
from policy_agent.collectors.rss import RSSCollector
from policy_agent.collectors.statcan import StatCanScheduleCollector

NOW = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)


def _source(source_id: str, collector: str, url: str, **extra):
    return {
        "id": source_id,
        "name": source_id,
        "institution": source_id,
        "jurisdiction": "CA",
        "collector": collector,
        "url": url,
        **extra,
    }


def test_statcan_schedule(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/statcan.json"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("statcan_schedule.json").read_bytes()})
    result = StatCanScheduleCollector().collect(
        _source("statcan", "statcan_schedule", url, timezone="America/Toronto", default_time="08:30"),
        client,
        NOW,
    )
    assert result.status == "ok"
    assert len(result.events) == 2
    cpi = result.events[0]
    assert cpi.event_type == "economic_release"
    assert cpi.start_at.isoformat() == "2026-07-20T12:30:00+00:00"
    assert "inflation" in cpi.topics


def test_rss_scheduled_event_uses_event_date_not_feed_date(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/upcoming-feed.xml"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("boc_upcoming.xml").read_bytes()})
    result = RSSCollector().collect(
        _source(
            "boc",
            "rss",
            url,
            timezone="America/Toronto",
            date_semantics="scheduled",
            default_time="09:00",
        ),
        client,
        NOW,
    )
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "monetary_policy_decision"
    assert event.start_at.isoformat() == "2026-07-15T13:45:00+00:00"
    assert event.published_at.isoformat() == "2026-07-13T12:00:00+00:00"


def test_bcuc_deadline(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/bcuc/deadlines"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("bcuc_deadlines.html").read_bytes()})
    result = BCUCDeadlinesCollector().collect(
        _source("bcuc-deadlines", "bcuc_deadlines", url, jurisdiction="BC", timezone="America/Vancouver"),
        client,
        NOW,
    )
    assert len(result.events) == 1
    event = result.events[0]
    assert event.lifecycle == "due"
    assert event.start_at.isoformat() == "2026-07-25T00:00:00+00:00"
    assert "1391" in str(event.source_url)


def test_bcuc_anticipated_quarter_and_month(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/bcuc/anticipated"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("bcuc_anticipated.html").read_bytes()})
    result = BCUCAnticipatedFilingsCollector().collect(
        _source("bcuc-anticipated", "bcuc_anticipated", url, jurisdiction="BC", timezone="America/Vancouver"),
        client,
        NOW,
    )
    assert len(result.events) == 2
    hydro = next(item for item in result.events if "BC Hydro" in item.title)
    assert hydro.confidence == "expected"
    assert hydro.start_at.isoformat() == "2026-07-01T16:00:00+00:00"
    assert hydro.end_at.isoformat() == "2026-10-01T06:59:00+00:00"


def test_bcuc_confidential_docket_is_metadata_only(fixture_dir: Path) -> None:
    source = _source(
        "bcuc-proceedings",
        "bcuc_proceedings",
        "https://www.bcuc.com/OurWork/Proceedings",
        jurisdiction="BC",
        timezone="America/Vancouver",
    )
    events, warnings = BCUCProceedingsCollector()._parse_proceeding(
        source=source,
        now=NOW,
        application_id="1391",
        url="https://www.bcuc.com/OurWork/ViewProceeding?applicationid=1391",
        content=fixture_dir.joinpath("bcuc_proceeding.html").read_bytes(),
    )
    assert not warnings
    confidential = next(item for item in events if item.identifiers.get("exhibit_id") == "B-8")
    public = next(item for item in events if item.identifiers.get("exhibit_id") == "B-7")
    assert confidential.metadata_only is True
    assert "B-8-confidential.pdf" not in str(confidential.source_url)
    assert str(confidential.source_url).endswith("applicationid=1391")
    assert "B-7-public-evidence.pdf" in str(public.source_url)


def test_federal_consultation(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/consultations.csv"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("consultations.csv").read_bytes()})
    result = FederalConsultationsCollector().collect(
        _source("consultations", "federal_consultations", url, timezone="America/Toronto"), client, NOW
    )
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "consultation"
    assert event.lifecycle == "open"
    assert event.end_at.isoformat() == "2026-08-16T03:59:00+00:00"
    assert event.raw["high_profile"] == "Yes"


def test_bc_laws_regulation(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/bclaws/2026cumulati"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("bc_laws.html").read_bytes()})
    result = BCLawsRegulationsCollector().collect(
        _source("bc-regs", "bc_laws_regulations", url, jurisdiction="BC", timezone="America/Vancouver"),
        client,
        NOW,
    )
    assert len(result.events) == 1
    event = result.events[0]
    assert event.identifiers["bc_regulation"] == "94/2026"
    assert event.start_at.isoformat() == "2026-08-01T07:00:00+00:00"


def test_html_date_list_uses_year_heading(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/elections"
    client = fake_client_cls(tmp_path, {url: fixture_dir.joinpath("elections_finance.html").read_bytes()})
    result = HTMLDateListCollector().collect(
        _source(
            "elections",
            "html_date_list",
            url,
            timezone="America/Toronto",
            force_event_type="filing_deadline",
            lifecycle="due",
            default_time="17:00",
            item_tags=["li"],
        ),
        client,
        NOW,
    )
    assert len(result.events) == 2
    assert result.events[0].start_at.year == 2026
    assert all(item.lifecycle == "due" for item in result.events)
