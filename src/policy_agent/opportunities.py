from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .models import Change, Event, Opportunity, ScoreComponent
from .utils import clean_text, stable_id

DEFAULT_BASE_SCORES = {
    "monetary_policy_decision": 70,
    "fiscal_release": 65,
    "regulatory_decision": 62,
    "final_regulation": 58,
    "economic_release": 52,
    "proposed_regulation": 50,
    "consultation": 46,
    "committee_meeting": 44,
    "filing_deadline": 42,
    "regulatory_timetable": 40,
    "evidence_filing": 38,
    "regulatory_order": 38,
    "speech": 30,
    "source_update": 25,
    "policy_event": 28,
}

DEFAULT_TOPIC_BONUS = {
    "monetary_policy": 10,
    "inflation": 8,
    "fiscal": 8,
    "energy": 8,
    "housing": 7,
    "growth": 6,
    "labour": 6,
    "regulation": 5,
    "trade": 5,
    "competition": 5,
    "environment": 4,
    "health": 4,
    "immigration": 4,
    "indigenous": 4,
    "transportation": 3,
}


def _event_relevant_at(event: Event, now: datetime) -> tuple[datetime | None, datetime | None]:
    deadline = None
    if event.event_type == "consultation" and event.end_at and event.end_at >= now:
        return event.end_at, event.end_at
    if event.lifecycle == "due" and event.start_at:
        return event.start_at, event.start_at
    if event.start_at and event.start_at >= now - timedelta(days=1):
        return event.start_at, event.end_at
    if event.end_at and event.end_at >= now:
        return event.end_at, event.end_at
    return event.published_at or event.start_at or event.end_at, deadline


def _horizon(event: Event, relevant_at: datetime | None, now: datetime, change_type: str) -> str:
    # Future milestones retain their preparation horizon even when first discovered today.
    if relevant_at and relevant_at >= now:
        days = (relevant_at - now).total_seconds() / 86400
        if days <= 14:
            return "execution"
        if days <= 30:
            return "preparation"
        if days <= 90:
            return "scouting"
    if event.published_at:
        age = now - event.published_at
        if change_type in {"new", "changed"} and timedelta(days=-1) <= age <= timedelta(days=3):
            return "react_now"
        if timedelta(0) <= age <= timedelta(days=14):
            return "second_wave"
    return "monitor"


def _proximity_points(event: Event, relevant_at: datetime | None, now: datetime) -> tuple[int, str]:
    if relevant_at and relevant_at >= now:
        days = (relevant_at - now).total_seconds() / 86400
        if days <= 2:
            return 15, "Event or deadline is within 48 hours."
        if days <= 7:
            return 12, "Event or deadline is within seven days."
        if days <= 14:
            return 9, "Event or deadline is within the execution window."
        if days <= 30:
            return 6, "Event is within the preparation window."
        if days <= 90:
            return 3, "Event is within the scouting window."
    if event.published_at:
        age_days = (now - event.published_at).total_seconds() / 86400
        if 0 <= age_days <= 2:
            return 15, "Publication is less than 48 hours old."
        if age_days <= 7:
            return 10, "Publication is within the first reaction week."
        if age_days <= 14:
            return 5, "Publication remains within a second-wave window."
    return 0, "No near-term date signal."


def _hook_type(event: Event) -> str:
    mapping = {
        "monetary_policy_decision": "scheduled_policy_decision",
        "economic_release": "fresh_evidence",
        "fiscal_release": "fiscal_accountability",
        "regulatory_decision": "decision_reaction",
        "regulatory_order": "regulatory_process",
        "evidence_filing": "regulatory_record",
        "filing_deadline": "pre_deadline_accountability",
        "regulatory_timetable": "advance_regulatory_hook",
        "consultation": "consultation_deadline",
        "proposed_regulation": "regulatory_comment_window",
        "final_regulation": "legal_change",
        "committee_meeting": "political_process",
        "speech": "agenda_signal",
        "source_update": "calendar_or_agenda_change",
    }
    return mapping.get(event.event_type, "policy_development")


def _format_date(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "an unspecified date"
    local = value.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%A, %B %-d, %Y")


def _why_now(event: Event, relevant_at: datetime | None, now: datetime, timezone_name: str, change_type: str) -> str:
    date_text = _format_date(relevant_at, timezone_name)
    if event.lifecycle == "due" or (event.event_type == "consultation" and event.end_at):
        return f"The public filing or consultation window reaches its deadline on {date_text}."
    if relevant_at and relevant_at >= now:
        days = max(0, round((relevant_at - now).total_seconds() / 86400))
        return f"The event is scheduled for {date_text}, approximately {days} day{'s' if days != 1 else ''} away."
    if event.published_at:
        if change_type in {"new", "changed"}:
            return f"A new or materially changed official publication was detected from {event.institution}."
        return "The official publication is recent enough for a follow-up or second-wave argument."
    return "The official source changed, creating a new agenda or calendar signal."


def _angle_prompts(event: Event) -> list[str]:
    mapping = {
        "monetary_policy_decision": [
            "Which assumption about inflation, growth, or financial conditions is most contestable?",
            "What would the decision mean specifically for households, investment, and policy choices in British Columbia?",
            "What should governments avoid doing if fiscal policy is working against monetary policy?",
        ],
        "economic_release": [
            "Does the new evidence confirm or contradict the prevailing policy narrative?",
            "Which regional, sectoral, or distributional effect is likely to be missed in the first coverage?",
            "What policy response follows from the data, and what response would be premature?",
        ],
        "fiscal_release": [
            "How do actual results compare with the government's stated fiscal plan?",
            "Which spending, revenue, or debt trend deserves more scrutiny than the headline balance?",
            "What trade-off should be made explicit before the next fiscal decision?",
        ],
        "regulatory_decision": [
            "What does the decision resolve, and what material issue remains unsettled?",
            "Who bears the cost, risk, or benefit under the ruling?",
            "Does the reasoning establish a precedent for future utility or regulatory files?",
        ],
        "regulatory_order": [
            "Which procedural choice is likely to shape the eventual substantive result?",
            "What evidence or stakeholder perspective is now required or excluded?",
            "What should observers watch before the next regulatory milestone?",
        ],
        "evidence_filing": [
            "Which assumption or claim in the filing is most important to test?",
            "What new fact changes the likely direction of the proceeding?",
            "What evidence remains absent despite the filing?",
        ],
        "filing_deadline": [
            "Which unresolved issue must be placed on the public record before the deadline?",
            "What position are key parties likely to take, and what would be missing if they remain silent?",
            "How could this filing alter the regulator's eventual choice?",
        ],
        "consultation": [
            "What policy choice is the consultation actually asking the public to make?",
            "Which trade-off or affected group is understated in the consultation framing?",
            "What concrete recommendation should be on the record before the window closes?",
        ],
        "proposed_regulation": [
            "What problem is the proposal trying to solve, and is the mechanism proportionate?",
            "Which incentives or compliance costs are likely to produce unintended effects?",
            "What amendment should be made before the regulation is finalized?",
        ],
        "final_regulation": [
            "What changes legally or operationally, and on what effective date?",
            "Who is affected differently than the government's summary suggests?",
            "What early indicator would show whether the regulation is working as intended?",
        ],
        "committee_meeting": [
            "Which question should committee members put to witnesses?",
            "What evidence or constituency is missing from the scheduled discussion?",
            "Does the meeting signal that the issue is moving toward legislation, spending, or a formal report?",
        ],
        "source_update": [
            "What changed in the calendar or agenda and why does the timing matter?",
            "Does the change indicate acceleration, delay, or a shift in institutional priority?",
        ],
    }
    return mapping.get(
        event.event_type,
        [
            "What changed, and why does it matter now?",
            "Which policy trade-off is not obvious from the official announcement?",
            "What specific recommendation can be tied to this event?",
        ],
    )


def identify_opportunities(
    events: list[Event],
    changes: list[Change],
    now: datetime,
    settings: dict[str, Any],
    rules: dict[str, Any],
) -> list[Opportunity]:
    change_map = {change.event_id: change.change_type for change in changes}
    base_scores = {**DEFAULT_BASE_SCORES, **rules.get("event_type_base", {})}
    topic_bonus = {**DEFAULT_TOPIC_BONUS, **rules.get("topic_bonus", {})}
    source_bonus = rules.get("source_bonus", {})
    priority_keywords: dict[str, int] = rules.get("priority_keywords", {})
    threshold = int(rules.get("minimum_score", 30))
    timezone_name = settings.get("display_timezone", "America/Vancouver")
    opportunities: list[Opportunity] = []

    for event in events:
        relevant_at, deadline_at = _event_relevant_at(event, now)
        change_type = change_map.get(event.id, "unchanged")
        horizon = _horizon(event, relevant_at, now, change_type)
        components: list[ScoreComponent] = []

        base = int(base_scores.get(event.event_type, base_scores.get("policy_event", 28)))
        components.append(ScoreComponent(name="event_type", points=base, reason=f"Base weight for {event.event_type}."))

        proximity, proximity_reason = _proximity_points(event, relevant_at, now)
        if proximity:
            components.append(ScoreComponent(name="proximity", points=proximity, reason=proximity_reason))

        novelty = 12 if change_type == "new" else 15 if change_type == "changed" else 0
        if novelty:
            components.append(
                ScoreComponent(name="novelty", points=novelty, reason=f"Event is {change_type} since the prior run.")
            )

        topic_points = min(16, sum(int(topic_bonus.get(topic, 0)) for topic in event.topics))
        if topic_points:
            components.append(
                ScoreComponent(
                    name="topic_fit", points=topic_points, reason=f"Priority topics: {', '.join(event.topics)}."
                )
            )

        source_points = int(source_bonus.get(event.source_id, 0))
        if source_points:
            components.append(
                ScoreComponent(
                    name="source_priority", points=source_points, reason=f"Priority source: {event.source_name}."
                )
            )

        text = clean_text(f"{event.title} {event.description or ''}").lower()
        keyword_points = 0
        matched_keywords: list[str] = []
        for keyword, points in priority_keywords.items():
            if keyword.lower() in text:
                keyword_points += int(points)
                matched_keywords.append(keyword)
        keyword_points = min(keyword_points, 15)
        if keyword_points:
            components.append(
                ScoreComponent(
                    name="keyword_signal", points=keyword_points, reason=f"Matched: {', '.join(matched_keywords[:6])}."
                )
            )

        if str(event.raw.get("high_profile", "")).lower() in {"yes", "true", "1", "high"}:
            components.append(
                ScoreComponent(
                    name="high_profile", points=5, reason="The official registry marks the item as high profile."
                )
            )

        if event.metadata_only:
            components.append(
                ScoreComponent(name="metadata_limit", points=-4, reason="Only public docket metadata is available.")
            )

        if bool(event.raw.get("initial_snapshot")):
            components.append(
                ScoreComponent(
                    name="initial_snapshot",
                    points=-15,
                    reason="This is the baseline page snapshot, not a detected subsequent change.",
                )
            )

        total = max(0, min(100, sum(component.points for component in components)))
        if total < threshold:
            continue
        if horizon == "monitor" and change_type == "unchanged" and total < int(rules.get("monitor_minimum_score", 55)):
            continue

        opportunities.append(
            Opportunity(
                id=f"opp:{stable_id(event.id, event.content_hash, horizon)}",
                event_id=event.id,
                event_title=event.title,
                source_id=event.source_id,
                source_name=event.source_name,
                source_url=event.source_url,
                institution=event.institution,
                jurisdiction=event.jurisdiction,
                event_type=event.event_type,
                hook_type=_hook_type(event),
                horizon=horizon,
                opportunity_score=total,
                score_components=components,
                why_now=_why_now(event, relevant_at, now, timezone_name, change_type),
                angle_prompts=_angle_prompts(event),
                topics=event.topics,
                relevant_at=relevant_at,
                deadline_at=deadline_at,
                published_at=event.published_at,
                confidence=event.confidence,
                change_type=change_type,
                generated_at=now,
            )
        )

    horizon_order = {"react_now": 0, "execution": 1, "preparation": 2, "scouting": 3, "second_wave": 4, "monitor": 5}
    opportunities.sort(
        key=lambda item: (
            horizon_order[item.horizon],
            -item.opportunity_score,
            item.relevant_at or datetime.max.replace(tzinfo=timezone.utc),
            item.event_title.lower(),
        )
    )
    return opportunities
