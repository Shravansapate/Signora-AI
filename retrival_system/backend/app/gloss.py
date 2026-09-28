"""Typed railway sign construction, distinct from English token lookup.

Default rules are a configurable engineering draft, not validated ISL grammar.
Reviewed database recipes remain authoritative when the existing compiler can use them.
"""

import re
from dataclasses import asdict, dataclass

from app.announcement_schema import DevelopmentGlossPolicy
from app.lexical import TOKEN_RE, normalize_domain
from app.meaning import normalize


@dataclass(frozen=True)
class GlossUnit:
    token: str
    kind: str = "LEXEME"
    role: str = "event"


def recover_lexical(normalized):
    """Recover independent slots without inventing an event or reordering unknown clauses.

    Function words stay in phrase lookup but are omitted if no phrase consumes them.
    Direction, negation and temporal relationships remain meaningful lexical units.
    """
    tokens = TOKEN_RE.findall(normalized)
    units, slots = [], {}
    grammar = {"at", "on", "the", "a", "an", "is", "are", "was", "were", "been", "has"}
    event_forms = {
        "arrive": "ARRIVE",
        "arrives": "ARRIVE",
        "arriving": "ARRIVE",
        "arrived": "ARRIVE",
        "depart": "DEPART",
        "departs": "DEPART",
        "departing": "DEPART",
        "departed": "DEPART",
        "delay": "DELAY",
        "delayed": "DELAY",
        "cancel": "CANCEL",
        "cancelled": "CANCEL",
    }
    events = []
    for i, token in enumerate(tokens):
        previous = tokens[i - 1] if i else None
        label = previous
        if previous in {"number", "no"} and i > 1:
            label = tokens[i - 2]
        kind = "FUNCTION" if token in grammar else "LEXICAL"
        if token in {"number", "no"} and previous in {"train", "platform"}:
            kind = "FUNCTION"
        if re.fullmatch(r"\d+[a-z]{0,2}", token):
            kind = (
                "IDENTIFIER"
                if label == "train"
                else "PLATFORM_IDENTIFIER"
                if label == "platform"
                else "NUMBER"
            )
            if label in {"train", "platform"}:
                slots.setdefault(f"{label}_identifier", []).append(token)
        elif ":" in token:
            kind = "CLOCK_TIME"
            slots.setdefault("clock_time", []).append(token)
        if token in event_forms:
            events.append(event_forms[token])
        if previous in {"from", "to"} and token != "platform":
            slots.setdefault(
                "source_station" if previous == "from" else "destination_station", []
            ).append(token)
        if token in {"minute", "minutes", "hour", "hours"} and previous and previous.isdigit():
            slots.setdefault("delay_duration", []).append(f"{previous} {token}")
        units.append(GlossUnit(token.upper(), kind, "lexical"))
    partial = {
        "slots": slots,
        "events": events,
        "polarity": "NEGATIVE" if "not" in tokens else None,
    }
    return units, partial


def construct_gloss(meaning, policy: DevelopmentGlossPolicy):
    if meaning is None:
        return []
    groups = {name: [] for name in policy.order}

    def sign(token, group, kind="LEXEME"):
        groups[group].append(
            GlossUnit(
                policy.lexical_forms.get(token, token) if kind == "LEXEME" else token, kind, group
            )
        )

    def slot(name, group, label=None):
        value = meaning.slots.get(name)
        if not value:
            return
        if label:
            sign(label, group)
        if value.kind == "NAME":
            # Display names, not configured database entity IDs, are lexical input.
            sign(normalize(value.raw)[0].upper(), group, "NAME")
        elif value.kind == "TRAIN_IDENTIFIER":
            sign(value.value, group, "IDENTIFIER")
        elif value.kind == "PLATFORM_IDENTIFIER":
            sign(value.value, group, "PLATFORM_IDENTIFIER")
        elif value.kind == "CLOCK_TIME":
            # A clock is not an arbitrary integer. Without an exact realization,
            # report it missing instead of dropping AM/PM or the colon.
            sign(value.raw.upper(), group, "CLOCK_TIME")
        else:
            sign(value.raw.upper(), group, "NUMBER")

    sign("TRAIN", "entity")
    slot("train_identifier", "entity")
    slot("train_name", "entity")
    relations = getattr(meaning, "relations", {})
    for role, marker in (("source", "FROM"), ("destination", "TO")):
        if role in meaning.slots:
            if relations.get(role) != "event_location":
                sign(marker, role)
            slot(role, role, "STATION")
    if "platform_identifier" in meaning.slots:
        relation = relations.get("platform_identifier", "location")
        if relation == "origin" and meaning.intent == "TRAIN_ARRIVAL":
            sign("FROM", "location")
        elif relation == "destination" and meaning.intent == "TRAIN_DEPARTURE":
            sign("TO", "location")
        slot("platform_identifier", "location", "PLATFORM")
    for name, marker in (("old_platform", "FROM"), ("new_platform", "TO")):
        if name in meaning.slots:
            sign(marker, "location")
            slot(name, "location", "PLATFORM")
    temporal = meaning.temporal_state
    if temporal.startswith("SCHEDULED_"):
        sign("FUTURE", "time")
    elif temporal.startswith("ALREADY_"):
        sign("ALREADY", "time")
    elif temporal in {"ARRIVING_NOW", "DEPARTING_NOW"}:
        sign("NOW", "time")
    if getattr(meaning, "time_modifier", None):
        sign(meaning.time_modifier.upper(), "time")
    slot("clock_time", "time", "TIME")
    if meaning.polarity == "NEGATIVE":
        sign("NOT", "polarity")
    sign(
        {
            "TRAIN_ARRIVAL": "ARRIVE",
            "TRAIN_DEPARTURE": "DEPART",
            "TRAIN_DELAY": "DELAY",
            "TRAIN_CANCELLATION": "CANCEL",
            "PLATFORM_CHANGE": "CHANGE",
        }[meaning.intent],
        "event",
    )
    if "delay_duration" in meaning.slots:
        duration = meaning.slots["delay_duration"]
        match = re.fullmatch(r"([0-9]+) (hours?|minutes?)", duration.value, re.IGNORECASE)
        if match:
            sign(match[1], "duration", "NUMBER")
            sign("HOUR" if match[2].lower().startswith("hour") else "MINUTE", "duration")
        else:
            sign(duration.raw.upper(), "duration", "DURATION")
    return [unit for group in policy.order for unit in groups[group]]


def translation_trace(original, normalized, meaning, issues, units, policy, *, construction=None):
    _, _, corrections = normalize_domain(*normalize(original))
    partial = recover_lexical(normalized)[1] if meaning is None else None
    return {
        "original_input": original,
        "normalized_input": normalized,
        "english_tokens": normalized.split(),
        "semantic_parse": meaning.model_dump(mode="json") if meaning else None,
        "translation_status": "DOMAIN_DRAFT" if meaning else "LEXICAL_RECOVERY",
        "normalization_corrections": corrections,
        "partial_semantics": partial,
        "semantic_status": "COMPLETE"
        if meaning
        else "PARTIAL"
        if partial and any(partial.values())
        else "UNSUPPORTED",
        "construction": construction
        or {
            "source": "STATION_DOMAIN_RULES" if meaning else "LEXICAL_RECOVERY",
            "rule_id": policy.rule_id if meaning else None,
            "linguistically_validated": False,
            "order": policy.order if meaning else ["input_order"],
        },
        "translation_issues": [item.model_dump(mode="json") for item in issues],
        "gloss_tokens": [unit.token for unit in units],
        "gloss_units": [asdict(unit) for unit in units],
        "retrieval_tokens": [unit.token for unit in units],
        # Compatibility key now deliberately reflects gloss, never English.
        "tokens": [unit.token for unit in units],
    }
