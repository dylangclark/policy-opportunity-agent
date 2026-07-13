from __future__ import annotations

from datetime import datetime, timezone

from policy_agent.collectors.common import build_event
from policy_agent.models import Change
from policy_agent.opportunities import identify_opportunities

NOW = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)
SOURCE = {
    "id": "boc-upcoming",
    "name": "Bank of Canada upcoming events",
    "institution": "Bank of Canada",
    "jurisdiction": "CA",
    "url": "https://example.test/boc",
    "timezone": "America/Toronto",
}


def test_scheduled_event_stays_in_execution_horizon() -> None:
    event = build_event(
        source=SOURCE,
        now=NOW,
        canonical_key="rate-2026-07-15",
        title="Interest rate announcement",
        event_type="monetary_policy_decision",
        lifecycle="scheduled",
        start_at=datetime(2026, 7, 15, 13, 45, tzinfo=timezone.utc),
        published_at=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        topics=["monetary_policy", "inflation"],
    )
    changes = [
        Change(event_id=event.id, source_id=event.source_id, change_type="new", title=event.title, detected_at=NOW)
    ]
    opportunities = identify_opportunities(
        [event],
        changes,
        NOW,
        {"display_timezone": "America/Vancouver"},
        {"minimum_score": 1, "source_bonus": {"boc-upcoming": 5}},
    )
    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.horizon == "execution"
    assert opportunity.opportunity_score > 80
    payload = opportunity.model_dump()
    for excluded in ("author", "outlet", "owner", "approval", "draft", "pitch_status"):
        assert excluded not in payload
