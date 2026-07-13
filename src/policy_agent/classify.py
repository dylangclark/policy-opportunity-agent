from __future__ import annotations

import re
from typing import Any

from .utils import clean_text, unique_preserve_order

EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "monetary_policy_decision",
        ("interest rate announcement", "policy rate", "overnight rate", "monetary policy report"),
    ),
    (
        "economic_release",
        (
            "consumer price index",
            "gross domestic product",
            "labour force",
            "employment",
            "retail trade",
            "manufacturing",
            "international trade",
            "building permits",
            "housing starts",
        ),
    ),
    ("fiscal_release", ("budget", "fiscal monitor", "economic and fiscal update", "public accounts", "estimates")),
    ("regulatory_decision", ("decision", "final order", "approves", "rejects", "determination")),
    ("regulatory_order", ("order", "procedural")),
    (
        "evidence_filing",
        (
            "evidence",
            "application",
            "response to",
            "information request",
            "ir no.",
            "argument",
            "submission",
            "undertaking",
        ),
    ),
    ("consultation", ("consultation", "engagement", "public input", "comment period", "feedback")),
    ("proposed_regulation", ("proposed regulation", "prepublication", "part i")),
    ("final_regulation", ("official regulation", "enacts", "amends", "regulation", "part ii")),
    ("committee_meeting", ("committee meeting", "meeting notice", "witnesses", "study of")),
    ("speech", ("speech", "remarks", "appearance")),
]

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "monetary_policy": ("bank of canada", "interest rate", "overnight rate", "inflation target", "monetary policy"),
    "inflation": ("consumer price index", "cpi", "inflation", "prices"),
    "labour": ("employment", "unemployment", "labour force", "wages", "payroll"),
    "growth": ("gross domestic product", "gdp", "economic growth", "productivity"),
    "fiscal": ("budget", "deficit", "debt", "fiscal", "tax", "public accounts", "estimates"),
    "energy": (
        "electricity",
        "natural gas",
        "bc hydro",
        "fortisbc",
        "energy",
        "pipeline",
        "utility",
        "utilities",
        "rate design",
    ),
    "housing": ("housing", "home", "rent", "mortgage", "building permit", "residential"),
    "regulation": ("regulation", "regulatory", "order in council", "compliance", "tariff"),
    "environment": ("climate", "emissions", "wildfire", "drought", "forest", "environment", "carbon"),
    "trade": ("trade", "tariff", "export", "import", "customs", "supply chain"),
    "health": ("health", "hospital", "pharmacare", "medical", "public health"),
    "immigration": ("immigration", "refugee", "temporary resident", "international student"),
    "indigenous": ("first nation", "indigenous", "treaty", "aboriginal", "métis", "inuit"),
    "transportation": ("transport", "transit", "rail", "port", "airport", "road", "ferry"),
    "competition": ("competition", "productivity", "investment", "competitiveness", "market power"),
}


def infer_event_type(text: str, default: str = "policy_event") -> str:
    lowered = clean_text(text).lower()
    for event_type, phrases in EVENT_RULES:
        if any(phrase in lowered for phrase in phrases):
            return event_type
    return default


def infer_topics(text: str, extra_rules: dict[str, list[str]] | None = None) -> list[str]:
    lowered = clean_text(text).lower()
    rules: dict[str, tuple[str, ...] | list[str]] = dict(TOPIC_KEYWORDS)
    if extra_rules:
        rules.update(extra_rules)
    topics: list[str] = []
    for topic, keywords in rules.items():
        for keyword in keywords:
            pattern = r"\b" + re.escape(keyword.lower()) + r"\b" if keyword.isalnum() else re.escape(keyword.lower())
            if re.search(pattern, lowered):
                topics.append(topic)
                break
    return unique_preserve_order(topics)


def source_value(source: dict[str, Any], key: str, default: Any = None) -> Any:
    value = source.get(key, default)
    return default if value is None else value
