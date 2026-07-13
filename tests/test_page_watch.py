from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from policy_agent.collectors.page_watch import PageWatchCollector

NOW = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)


def test_page_watch_id_is_stable_when_content_changes(fixture_dir: Path, fake_client_cls, tmp_path: Path) -> None:
    url = "https://example.test/forward-plan"
    client = fake_client_cls(
        tmp_path,
        {
            url: [
                fixture_dir.joinpath("page_watch_v1.html").read_bytes(),
                fixture_dir.joinpath("page_watch_v2.html").read_bytes(),
            ]
        },
    )
    source = {
        "id": "forward-plan",
        "name": "Forward plan",
        "institution": "Agency",
        "jurisdiction": "CA",
        "collector": "page_watch",
        "url": url,
    }
    first = PageWatchCollector().collect(source, client, NOW)
    second = PageWatchCollector().collect(source, client, NOW)
    assert first.events[0].id == second.events[0].id
    assert first.events[0].content_hash != second.events[0].content_hash
    assert first.events[0].raw["initial_snapshot"] is True
    assert second.events[0].raw["initial_snapshot"] is False
