from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from policy_agent.collectors.base import CollectResult
from policy_agent.collectors.common import build_event
from policy_agent.config import AgentConfig
from policy_agent.pipeline import run_pipeline


class SequenceCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, source, client, now):
        self.calls += 1
        if self.calls == 1:
            event = build_event(
                source=source,
                now=now,
                canonical_key="stable-event",
                title="Budget update",
                event_type="fiscal_release",
                lifecycle="published",
                published_at=now,
                topics=["fiscal"],
            )
            return CollectResult(source_id=source["id"], events=[event], status="ok", http_status=200)
        return CollectResult(source_id=source["id"], status="failed", error="temporary outage")


def test_failed_source_retains_last_good_data(monkeypatch, tmp_path: Path) -> None:
    collector = SequenceCollector()
    monkeypatch.setattr("policy_agent.pipeline.get_collector", lambda name: collector)
    config = AgentConfig(
        settings={"contact_email": "test@example.com", "display_timezone": "America/Vancouver"},
        sources=[
            {
                "id": "test-source",
                "name": "Test source",
                "institution": "Test institution",
                "jurisdiction": "CA",
                "collector": "fake",
                "url": "https://example.test/source",
            }
        ],
    )
    output = tmp_path / "docs" / "data"
    state = tmp_path / ".state"
    first_now = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)
    run_pipeline(agent_config=config, rules={"minimum_score": 1}, output_dir=output, state_dir=state, now=first_now)
    manifest = run_pipeline(
        agent_config=config,
        rules={"minimum_score": 1},
        output_dir=output,
        state_dir=state,
        now=first_now + timedelta(hours=12),
    )
    events = json.loads((output / "events.json").read_text())["events"]
    statuses = json.loads((output / "source-status.json").read_text())["sources"]
    assert manifest.status == "failed"
    assert len(events) == 1
    assert statuses[0]["stale"] is True
    assert statuses[0]["retained_previous_count"] == 1
