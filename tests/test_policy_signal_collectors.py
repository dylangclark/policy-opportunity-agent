from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from policy_agent.collectors.bc_eao import BCEAOMilestonesCollector
from policy_agent.collectors.bc_orders import BCOrdersCollector
from policy_agent.collectors.federal_regulatory_plans import FederalRegulatoryPlansCollector
from policy_agent.collectors.house_reports import HouseCommitteeReportsCollector, parse_report_listing
from policy_agent.collectors.iaac import IAACMilestonesCollector


@dataclass
class FakeResult:
    url: str
    content: bytes
    status_code: int = 200
    headers: dict[str, str] | None = None
    not_modified: bool = False


class FakeClient:
    def __init__(self, routes: dict[str, str | bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> FakeResult:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"Unexpected URL: {url}")
        value = self.routes[url]
        content = value.encode("utf-8") if isinstance(value, str) else value
        return FakeResult(url=url, content=content, headers={"content-type": "text/html"})


def _source(**overrides: Any) -> dict[str, Any]:
    source = {
        "id": "test-source",
        "name": "Test source",
        "institution": "Test institution",
        "jurisdiction": "CA",
        "timezone": "America/Toronto",
        "url": "https://example.test/",
    }
    source.update(overrides)
    return source


def test_bc_orders_resume_extracts_policy_orders() -> None:
    browse = "https://www.bclaws.gov.bc.ca/civix/content/oic/oic_cur/?xsl=/templates/browse.xsl"
    resume = "https://www.bclaws.gov.bc.ca/civix/document/id/oic/oic_cur/2026resume28"
    client = FakeClient(
        {
            browse: f'<html><a href="{resume}">Resume 28</a></html>',
            resume: """
                <html><body>
                <h1>Resumé of Proclamations, Orders in Council and Ministerial Orders</h1>
                <p>July 1 – July 7, 2026</p>
                <p>Approved and Ordered July 2, 2026</p>
                <p>ORDER IN COUNCIL 300</p>
                <p>Statutory Authority: Utilities Commission, s. 3</p>
                <p>A special direction to the British Columbia Utilities Commission is made.</p>
                <p>MINISTERIAL ORDER 200</p>
                <p>Ministry Responsible: HOUSING</p>
                <p>Statutory Authority: Housing Supply, s. 2</p>
                <p>Effective August 1, 2026, a housing target order is made for Example City.</p>
                </body></html>
            """,
        }
    )
    result = BCOrdersCollector().collect(
        _source(
            id="bc-legal-orders",
            institution="King's Printer for British Columbia",
            jurisdiction="BC",
            timezone="America/Vancouver",
            url=browse,
            browse_urls=[browse],
            lookback_days=90,
            lookahead_days=550,
            minimum_signal_score=2,
        ),
        client,
        datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert result.status == "ok"
    assert len(result.events) == 2
    assert {event.identifiers["order_number"] for event in result.events} == {"300", "200"}
    housing = next(event for event in result.events if event.identifiers["order_number"] == "200")
    assert housing.start_at is not None
    assert housing.raw["source_format"] == "weekly_resume"


def test_forward_regulatory_plan_extracts_gazette_window() -> None:
    url = "https://example.test/plan"
    client = FakeClient(
        {
            url: """
                <html><main>
                <h2>Clean Electricity Regulations</h2>
                <p>The proposed regulations are expected to be pre-published in the
                Canada Gazette, Part I in fall 2026. Final publication in Canada Gazette,
                Part II is planned for spring 2027.</p>
                </main></html>
            """
        }
    )
    result = FederalRegulatoryPlansCollector().collect(
        _source(
            id="federal-forward-regulatory-initiatives",
            url=url,
            plan_pages=[{"institution": "Environment and Climate Change Canada", "url": url}],
            lookback_days=30,
            lookahead_days=730,
        ),
        client,
        datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert result.status == "ok"
    assert any(event.event_type == "proposed_regulation" for event in result.events)
    assert any(event.start_at is not None for event in result.events)


def test_bc_eao_extracts_referral_to_ministers() -> None:
    url = "https://www.projects.eao.gov.bc.ca/news"
    client = FakeClient(
        {
            url: """
                <html><body>
                <article>
                  <h3>Angus</h3>
                  <h4>Assessment complete and proposed project referred to ministers for decision</h4>
                  <p>June 17, 2026</p>
                  <p>The Environmental Assessment Office has completed its assessment.</p>
                  <a href="/p/angus/project-details">Project Info</a>
                </article>
                </body></html>
            """
        }
    )
    result = BCEAOMilestonesCollector().collect(
        _source(
            id="bc-eao-milestones",
            institution="B.C. Environmental Assessment Office",
            jurisdiction="BC",
            timezone="America/Vancouver",
            url=url,
            lookback_days=60,
            max_pages=1,
        ),
        client,
        datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert result.status == "ok"
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "regulatory_timetable"
    assert event.raw["high_profile"] is True
    assert "referred to ministers" in event.title.lower()


def test_iaac_extracts_public_comment_deadline() -> None:
    search_url = "https://iaac-aeic.gc.ca/050/evaluations/exploration?showMap=false"
    project_url = "https://iaac-aeic.gc.ca/050/evaluations/proj/90557/"
    client = FakeClient(
        {
            search_url: f"""
                <html><body>
                <article>
                  <a href="{project_url}">Project Trigon Pacific LPG Project</a>
                  <p>Location (Prince Rupert, British Columbia)
                  Assessment Type: Planning Phase for Impact Assessment
                  Status: In progress Reference Number: 90557
                  Last Modified: 2026-07-13</p>
                </article>
                </body></html>
            """,
            project_url: """
                <html><main>
                <h1>Trigon Pacific LPG Project</h1>
                <h2>Latest update</h2>
                <section>
                  <p>Monday, July 13, 2026</p>
                  <p>A public comment period is open. Comments are due August 12, 2026.</p>
                  <a href="/050/evaluations/document/123">Notice</a>
                </section>
                <h2>Project Details</h2>
                </main></html>
            """,
        }
    )
    result = IAACMilestonesCollector().collect(
        _source(
            id="iaac-major-projects",
            institution="Impact Assessment Agency of Canada",
            url=search_url,
            lookback_days=30,
            max_pages=1,
            max_projects=5,
        ),
        client,
        datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert result.status == "ok"
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "consultation"
    assert event.start_at is not None
    assert event.identifiers["reference_number"] == "90557"


def test_house_report_parser_and_response_due_event() -> None:
    listing_url = "https://www.ourcommons.ca/Committees/en/Work?show=reports"
    report_url = "https://www.ourcommons.ca/DocumentViewer/en/45-1/ENVI/report-7/"
    listing_html = f"""
        <html><body>
        <article>
          <a href="/Committees/en/ENVI">ENVI</a>
          <a href="{report_url}">Report 7 - Protecting Canadian residents from extreme weather events</a>
          <a href="{report_url}">Report 7 presented to the House</a>
          <time>Wednesday, June 17, 2026</time>
        </article>
        </body></html>
    """
    parsed = parse_report_listing(listing_html.encode(), listing_url)
    assert len(parsed) == 1
    assert parsed[0].committee == "ENVI"
    assert parsed[0].event_kind == "presented"

    client = FakeClient(
        {
            listing_url: listing_html,
            report_url: """
                <html><main>
                <h1>SEVENTH REPORT</h1>
                <p>Pursuant to Standing Order 109, the committee requests that the
                government table a comprehensive response to this report.</p>
                </main></html>
            """,
        }
    )
    result = HouseCommitteeReportsCollector().collect(
        _source(
            id="house-committee-reports",
            institution="House of Commons of Canada",
            url=listing_url,
            priority_committees=["ENVI"],
            max_pages=1,
            max_report_pages=5,
            parse_report_pdfs=False,
            lookback_days=180,
            response_horizon_days=240,
        ),
        client,
        datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert result.status == "ok"
    assert {event.raw["event_kind"] for event in result.events} == {
        "report_presented",
        "government_response_due",
    }
    due = next(event for event in result.events if event.raw["event_kind"] == "government_response_due")
    assert due.start_at is not None
    assert due.raw["response_due_date"] == "2026-10-15"
