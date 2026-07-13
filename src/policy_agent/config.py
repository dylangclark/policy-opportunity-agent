from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class AgentConfig:
    sources: list[dict[str, Any]]
    settings: dict[str, Any]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return payload


def load_agent_config(path: Path) -> AgentConfig:
    payload = load_yaml(path)
    sources = payload.get("sources", [])
    settings = payload.get("settings", {})
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")
    ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Each source must be a mapping")
        for required in ("id", "name", "collector", "url"):
            if not source.get(required):
                raise ValueError(f"Source is missing required field {required!r}: {source}")
        if source["id"] in ids:
            raise ValueError(f"Duplicate source id: {source['id']}")
        ids.add(source["id"])
    return AgentConfig(sources=sources, settings=settings)


def user_agent(settings: dict[str, Any]) -> str:
    contact = os.getenv("POLICY_AGENT_CONTACT_EMAIL") or settings.get("contact_email") or "replace-me@example.com"
    return f"policy-opportunity-agent/{settings.get('version', '0.1.0')} (+mailto:{contact})"
