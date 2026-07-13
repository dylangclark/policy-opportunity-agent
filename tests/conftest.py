from __future__ import annotations

from pathlib import Path

import pytest

from policy_agent.http import FetchResult


class FakeClient:
    def __init__(self, state_dir: Path, responses: dict[str, bytes | list[bytes]]) -> None:
        self.state_dir = state_dir
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"No fake responses left for {url}")
            content = value.pop(0)
        else:
            content = value
        content_type = "application/octet-stream"
        if url.endswith(".json"):
            content_type = "application/json; charset=utf-8"
        elif url.endswith(".csv"):
            content_type = "text/csv; charset=utf-8"
        elif url.endswith(".xml") or "feed" in url or "rss" in url:
            content_type = "application/rss+xml; charset=utf-8"
        elif b"<html" in content.lower():
            content_type = "text/html; charset=utf-8"
        return FetchResult(url=url, status_code=200, content=content, headers={"content-type": content_type})


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_client_cls():
    return FakeClient
