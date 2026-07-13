from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..http import HttpClient
from ..models import Event


@dataclass(slots=True)
class CollectResult:
    source_id: str
    events: list[Event] = field(default_factory=list)
    status: str = "ok"
    http_status: int | None = None
    not_modified: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class Collector(Protocol):
    def collect(
        self,
        source: dict[str, Any],
        client: HttpClient,
        now: datetime,
    ) -> CollectResult: ...


class CollectorError(RuntimeError):
    pass
