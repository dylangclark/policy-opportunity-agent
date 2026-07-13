from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ..classify import infer_topics
from ..http import HttpClient
from ..utils import atomic_write_json, clean_text, content_hash, read_json
from .base import CollectResult
from .common import build_event


class PageWatchCollector:
    """Emit an event only when the meaningful text or linked documents change."""

    def collect(self, source: dict[str, Any], client: HttpClient, now: datetime) -> CollectResult:
        result = client.get(source["url"], conditional_key=source["id"])
        if result.not_modified:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        soup = BeautifulSoup(result.content, "html.parser")
        for element in soup(["script", "style", "nav", "footer", "noscript"]):
            element.decompose()
        main = soup.find("main") or soup.find(id="main") or soup.body or soup
        text = clean_text(main.get_text(" ", strip=True))
        links = sorted(
            {
                str(anchor.get("href"))
                for anchor in main.find_all("a", href=True)
                if anchor.get("href") and not str(anchor.get("href")).startswith("#")
            }
        )
        digest = content_hash({"text": text, "links": links})
        state_path = Path(client.state_dir) / "page-watch.json"
        state = read_json(state_path, {})
        previous = state.get(source["id"])
        state[source["id"]] = {"hash": digest, "checked_at": now.isoformat()}
        atomic_write_json(state_path, state)

        if previous and previous.get("hash") == digest:
            return CollectResult(
                source_id=source["id"],
                status="not_modified",
                http_status=result.status_code,
                not_modified=True,
            )

        title = source.get("change_title") or f"{source.get('name', source['id'])} updated"
        event = build_event(
            source=source,
            now=now,
            canonical_key=f"page-watch|{source['id']}",
            title=title,
            source_url=result.url,
            event_type=source.get("default_event_type", "source_update"),
            lifecycle="updated",
            description=text[:2000] or None,
            published_at=now,
            confidence="confirmed",
            topics=infer_topics(text, source.get("topic_rules")),
            raw={"page_hash": digest, "link_count": len(links), "initial_snapshot": previous is None},
        )
        return CollectResult(
            source_id=source["id"],
            events=[event],
            status="ok",
            http_status=result.status_code,
        )
