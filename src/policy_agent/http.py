from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

import httpx

from .utils import atomic_write_json, read_json, utc_now

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    content: bytes
    headers: Mapping[str, str]
    not_modified: bool = False

    @property
    def text(self) -> str:
        encoding = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            encoding = content_type.rsplit("charset=", 1)[-1].split(";", 1)[0].strip()
        return self.content.decode(encoding, errors="replace")

    def json(self):
        return json.loads(self.text)


class HttpClient:
    """HTTP client with conditional requests, retries, and per-host pacing."""

    def __init__(
        self,
        state_dir: Path,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        retries: int = 2,
        min_host_interval_seconds: float = 0.35,
    ) -> None:
        self.state_dir = state_dir
        self.cache_path = state_dir / "http-cache.json"
        self.cache: dict[str, dict[str, str]] = read_json(self.cache_path, {})
        self.retries = retries
        self.min_host_interval_seconds = min_host_interval_seconds
        self._last_request_by_host: dict[str, float] = {}
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, application/rss+xml, application/atom+xml, text/html, text/csv, */*;q=0.5",
            },
        )

    def close(self) -> None:
        self.client.close()
        atomic_write_json(self.cache_path, self.cache)

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _pace(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        if not host:
            return
        last = self._last_request_by_host.get(host)
        if last is not None:
            remaining = self.min_host_interval_seconds - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_by_host[host] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        conditional_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        allow_not_modified: bool = True,
    ) -> FetchResult:
        request_headers: dict[str, str] = dict(headers or {})
        cache_key = conditional_key or url
        cached = self.cache.get(cache_key, {}) if allow_not_modified else {}
        if cached.get("etag"):
            request_headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            request_headers["If-Modified-Since"] = cached["last_modified"]

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                self._pace(url)
                response = self.client.get(url, headers=request_headers)
                if response.status_code == 304:
                    return FetchResult(
                        url=str(response.url),
                        status_code=304,
                        content=b"",
                        headers=response.headers,
                        not_modified=True,
                    )
                response.raise_for_status()
                self.cache[cache_key] = {
                    "etag": response.headers.get("etag", ""),
                    "last_modified": response.headers.get("last-modified", ""),
                    "checked_at": utc_now().isoformat(),
                }
                return FetchResult(
                    url=str(response.url),
                    status_code=response.status_code,
                    content=response.content,
                    headers=response.headers,
                )
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = min(2**attempt, 5)
                LOGGER.warning("Fetch failed for %s (attempt %s): %s", url, attempt + 1, exc)
                time.sleep(delay)
        assert last_error is not None
        raise last_error
