from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

SCHEMA_VERSION = "1.0"


class Event(BaseModel):
    """Normalized source event used by the opportunity engine and frontend."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    id: str
    canonical_key: str
    source_id: str
    source_name: str
    source_url: HttpUrl
    jurisdiction: str
    institution: str
    event_type: str
    lifecycle: str
    title: str
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    published_at: datetime | None = None
    source_timezone: str = "America/Toronto"
    all_day: bool = False
    confidence: Literal["confirmed", "expected", "inferred"] = "confirmed"
    topics: list[str] = Field(default_factory=list)
    identifiers: dict[str, str] = Field(default_factory=dict)
    metadata_only: bool = False
    first_seen_at: datetime
    last_seen_at: datetime
    content_hash: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ScoreComponent(BaseModel):
    name: str
    points: int
    reason: str


class Opportunity(BaseModel):
    """An editorial opportunity signal. No author, outlet, or workflow fields."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    id: str
    event_id: str
    event_title: str
    source_id: str
    source_name: str
    source_url: HttpUrl
    institution: str
    jurisdiction: str
    event_type: str
    hook_type: str
    horizon: Literal[
        "react_now",
        "execution",
        "preparation",
        "scouting",
        "second_wave",
        "monitor",
    ]
    opportunity_score: int = Field(ge=0, le=100)
    score_components: list[ScoreComponent]
    why_now: str
    angle_prompts: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    relevant_at: datetime | None = None
    deadline_at: datetime | None = None
    published_at: datetime | None = None
    confidence: Literal["confirmed", "expected", "inferred"] = "confirmed"
    change_type: Literal["new", "changed", "unchanged", "removed"] = "unchanged"
    generated_at: datetime


class Change(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str
    source_id: str
    change_type: Literal["new", "changed", "removed"]
    title: str
    detected_at: datetime
    changed_fields: list[str] = Field(default_factory=list)


class SourceStatus(BaseModel):
    schema_version: str = SCHEMA_VERSION
    source_id: str
    source_name: str
    collector: str
    url: HttpUrl
    status: Literal["ok", "not_modified", "partial", "failed", "disabled"]
    checked_at: datetime
    event_count: int = 0
    retained_previous_count: int = 0
    http_status: int | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    stale: bool = False


class ManifestFile(BaseModel):
    path: str
    sha256: str
    bytes: int
    count: int | None = None


class Manifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime
    run_id: str
    status: Literal["ok", "partial", "failed"]
    timezone: str
    horizons: dict[str, int]
    files: dict[str, ManifestFile]
    counts: dict[str, int]
    source_summary: dict[str, int]
