"""Conservative full-sentence English parser; never invents an ISL construction."""

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from app.announcement_schema import (
    DevelopmentMeaning,
    Issue,
    Meaning,
    Slot,
    StationDefinition,
    StructuredMeaning,
)
from app.lexical import normalize_domain

SLOT_TYPES = {
    "train_identifier": "TRAIN_IDENTIFIER",
    "platform_identifier": "PLATFORM_IDENTIFIER",
    "old_platform": "PLATFORM_IDENTIFIER",
    "new_platform": "PLATFORM_IDENTIFIER",
    "clock_time": "CLOCK_TIME",
    "delay_duration": "DURATION",
    "source": "NAME",
    "destination": "NAME",
    "train_name": "NAME",
}
TEMPORAL_INTENTS = {
    "ARRIVING_NOW": "TRAIN_ARRIVAL",
    "ALREADY_ARRIVED": "TRAIN_ARRIVAL",
    "SCHEDULED_ARRIVAL": "TRAIN_ARRIVAL",
    "DEPARTING_NOW": "TRAIN_DEPARTURE",
    "ALREADY_DEPARTED": "TRAIN_DEPARTURE",
    "SCHEDULED_DEPARTURE": "TRAIN_DEPARTURE",
    "DELAYED": "TRAIN_DELAY",
    "CANCELLED": "TRAIN_CANCELLATION",
    "CURRENT_CHANGE": "PLATFORM_CHANGE",
}
STATES = {
    "is arriving": "ARRIVING_NOW",
    "has arrived": "ALREADY_ARRIVED",
    "has already arrived": "ALREADY_ARRIVED",
    "will arrive": "SCHEDULED_ARRIVAL",
    "is departing": "DEPARTING_NOW",
    "has departed": "ALREADY_DEPARTED",
    "has already departed": "ALREADY_DEPARTED",
    "will depart": "SCHEDULED_DEPARTURE",
}
TRAIN = r"train (?:number |no\.? )?(?P<train_identifier>[0-9]{1,12}[a-z]{0,2})"
ROLES = r"(?: from (?P<source>[a-z][a-z .'-]{0,100}?) to (?P<destination>[a-z][a-z .'-]{0,100}?))?"
MOTION = re.compile(
    TRAIN + ROLES + r" (?P<state>is (?:not )?(?:arriving|departing)|has (?:not |already )?"
    r"(?:arrived|departed)|will (?:not )?(?:arrive|depart))"
    r" (?P<preposition>on|at|from) platform (?P<platform_identifier>[0-9]{1,6}[a-z]{0,2})"
    r"(?: at (?P<clock_time>[0-9]{1,2}:[0-9]{2}(?: ?[ap]m)?))?\.?"
)
CANCEL = re.compile(TRAIN + ROLES + r" (?:is|has been) (?P<neg>not )?cancelled\.?")
DELAY = re.compile(
    TRAIN
    + ROLES
    + r" is (?P<neg>not )?delayed by (?P<delay_duration>[0-9]{1,4} (?:minutes?|hours?))\.?"
)
CHANGE = re.compile(
    r"platform for " + TRAIN + r" has changed from (?P<old_platform>[0-9]{1,6}[a-z]{0,2})"
    r" to (?P<new_platform>[0-9]{1,6}[a-z]{0,2})\.?"
)

# The development grammar extends the same semantic parser, not the retriever.
# Anchored clauses keep unhandled relationships from disappearing as stopwords.
DOMAIN_STATES = {
    **STATES,
    "arrive": "ARRIVAL_UNSPECIFIED",
    "arrives": "ARRIVAL_UNSPECIFIED",
    "arriving": "ARRIVING_NOW",
    "arrived": "ALREADY_ARRIVED",
    "depart": "DEPARTURE_UNSPECIFIED",
    "departs": "DEPARTURE_UNSPECIFIED",
    "departing": "DEPARTING_NOW",
    "departed": "ALREADY_DEPARTED",
    "does arrive": "ARRIVAL_UNSPECIFIED",
    "does depart": "DEPARTURE_UNSPECIFIED",
}
DOMAIN_INTENTS = {
    **TEMPORAL_INTENTS,
    "ARRIVAL_UNSPECIFIED": "TRAIN_ARRIVAL",
    "DEPARTURE_UNSPECIFIED": "TRAIN_DEPARTURE",
}
DOMAIN_TRAIN = (
    r"(?:(?:the )?train(?: (?:number |no\.? )?"
    r"(?P<train_identifier>[0-9]{1,12}[a-z]{0,2}))?"
    r"|(?P<implicit_train_identifier>[0-9]{1,12}[a-z]{0,2}))"
)
DOMAIN_MOTION = re.compile(
    DOMAIN_TRAIN + ROLES + r" (?P<state>(?:is |has |has already |will |does )?(?:not )?"
    r"(?:arrives?|arriving|arrived|departs?|departing|departed))"
    + r"(?: (?P<time_modifier>shortly|soon|today|tomorrow|yesterday))?"
    + r"(?: (?:(?P<preposition>at|on|from|to) )?(?(preposition)|(?=platform ))"
    r"(?:platform (?:number )?"
    r"(?P<platform_identifier>[0-9]{1,6}[a-z]{0,2})|(?P<place>[a-z][a-z .'-]{0,150}?)))?"
    + r"(?: (?P<trailing_time>shortly|soon|today|tomorrow|yesterday))?\.?"
)
DOMAIN_DELAY = re.compile(
    DOMAIN_TRAIN + ROLES + r" (?:is|has been) (?P<neg>not )?delayed"
    r"(?: by (?P<delay_duration>[0-9]{1,4} (?:minutes?|hours?)))?\.?"
)
DOMAIN_CANCEL = re.compile(DOMAIN_TRAIN + ROLES + r" (?:is|has been) (?P<neg>not )?cancelled\.?")


def _domain_match(normalized):
    for pattern, temporal in (
        (DOMAIN_MOTION, None),
        (DOMAIN_DELAY, "DELAYED"),
        (DOMAIN_CANCEL, "CANCELLED"),
        (CHANGE, "CURRENT_CHANGE"),
    ):
        match = pattern.fullmatch(normalized)
        if match:
            state = match.groupdict().get("state", "").replace("not ", "")
            if temporal is None and state not in DOMAIN_STATES:
                continue
            place = match.groupdict().get("place")
            # Do not reinterpret an unhandled clause or time relation as a place name.
            if place and re.search(
                r"\b(?:and|but|not|before|after|between|platform|will|arrive|depart)\b", place
            ):
                continue
            return match, temporal or DOMAIN_STATES[state]
    return None, None


def normalize(text):
    """Normalize digits/case/whitespace while retaining original character offsets."""
    chars, spans = [], []
    for index, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                spans.append(index)
            continue
        value = str(unicodedata.decimal(char)) if char.isdecimal() else char.casefold()
        for normalized in value:
            chars.append(normalized)
            spans.append(index)
    if chars and chars[-1] == " ":
        chars.pop()
        spans.pop()
    return "".join(chars), spans


def meaning_hash(meaning):
    data = meaning.model_dump(mode="json")
    data["slots"] = {name: {"kind": s.kind, "value": s.value} for name, s in meaning.slots.items()}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_signature(intent, temporal, names):
    if TEMPORAL_INTENTS.get(temporal) != intent:
        return False
    required = {"train_identifier"}
    if intent in {"TRAIN_ARRIVAL", "TRAIN_DEPARTURE"}:
        required.add("platform_identifier")
    if intent == "TRAIN_DELAY":
        required.add("delay_duration")
    if intent == "PLATFORM_CHANGE":
        required.update({"old_platform", "new_platform"})
    allowed = required | {"source", "destination", "train_name"}
    if intent in {"TRAIN_ARRIVAL", "TRAIN_DEPARTURE"}:
        allowed.add("clock_time")
    return required <= set(names) <= allowed and (("source" in names) == ("destination" in names))


def _slot(name, raw, station, service_date, start=None, end=None):
    normalized, _ = normalize(raw)
    value, issue = normalized.upper(), None
    kind = SLOT_TYPES[name]
    if kind == "TRAIN_IDENTIFIER":
        suffix = "|".join(re.escape(s) for s in station.train_suffixes)
        pattern = rf"[0-9]{{{station.train_min_digits},{station.train_max_digits}}}"
        if suffix:
            pattern += f"(?:{suffix})?"
        if not re.fullmatch(pattern, value):
            issue = "Train identifier does not match the configured station format"
    elif kind == "PLATFORM_IDENTIFIER":
        if value not in station.platforms:
            issue = "Platform is absent from the configured station inventory"
    elif kind == "DURATION":
        match = re.fullmatch(r"([0-9]{1,4}) (minutes?|hours?)", normalized)
        if (
            not match
            or not 1 <= int(match[1]) <= 1440
            or (match[2].startswith("hour") and int(match[1]) > 24)
        ):
            issue = "Delay requires a positive bounded quantity and explicit minutes or hours"
        else:
            value = f"{int(match[1])} {'hours' if match[2].startswith('hour') else 'minutes'}"
    elif kind == "CLOCK_TIME":
        match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})(?: ?([ap]m))?", normalized)
        if not match or service_date is None:
            issue = "Clock time requires an explicit service date and valid time"
        else:
            hour, minute, period = int(match[1]), int(match[2]), match[3]
            if minute > 59 or (period and not 1 <= hour <= 12) or hour > 23:
                issue = "Invalid clock time"
            elif not period and 1 <= hour <= 12:
                issue = "Specify AM/PM for a clock time between 01:00 and 12:59"
            else:
                if period:
                    hour = hour % 12 + (12 if period == "pm" else 0)
                value = datetime(
                    service_date.year,
                    service_date.month,
                    service_date.day,
                    hour,
                    minute,
                    tzinfo=ZoneInfo("Asia/Kolkata"),
                ).isoformat()
    elif kind == "NAME":
        entities = station.trains if name == "train_name" else station.places
        matches = [
            e for e in entities if normalized in [normalize(n)[0] for n in [e.name, *e.aliases]]
        ]
        if len(matches) != 1:
            issue = (
                "Name requires an unambiguous configured entity; transliteration is not inferred"
            )
            value = normalized
        else:
            value = matches[0].id
    return (
        Slot(
            kind=kind, raw=raw, value=value, source_start=start, source_end=end, valid=issue is None
        ),
        Issue(code="INVALID_SLOT", detail=issue, slot=name) if issue else None,
    )


def parse_meaning(
    text,
    station_id,
    station: StationDefinition,
    service_date=None,
    structured=None,
    *,
    development=False,
):
    normalized, offsets = normalize(text)
    if development:
        normalized, offsets, _ = normalize_domain(normalized, offsets)
    raw_slots, spans, issues = {}, {}, []
    relations, event_form, time_modifier = {}, None, None
    if structured is not None:
        intent, temporal, polarity = (
            structured.intent,
            structured.temporal_state,
            structured.polarity,
        )
        raw_slots = structured.slots
    else:
        match, temporal = None, None
        for pattern, state in (
            (MOTION, None),
            (CANCEL, "CANCELLED"),
            (DELAY, "DELAYED"),
            (CHANGE, "CURRENT_CHANGE"),
        ):
            match = pattern.fullmatch(normalized)
            if match:
                temporal = state
                break
        if match is None and development:
            match, temporal = _domain_match(normalized)
        if match is None:
            return (
                normalized,
                None,
                [
                    Issue(
                        code="UNSUPPORTED_GRAMMAR",
                        detail="The complete sentence does not match a supported construction",
                    )
                ],
            )
        values = match.groupdict()
        if values.get("implicit_train_identifier"):
            values["train_identifier"] = values["implicit_train_identifier"]
        state = values.get("state", "")
        polarity = "NEGATIVE" if "not " in state or values.get("neg") else "POSITIVE"
        temporal = temporal or STATES[state.replace("not ", "")]
        intent = DOMAIN_INTENTS[temporal]
        event_form = state or temporal
        if (
            values.get("time_modifier")
            and values.get("trailing_time")
            and values["time_modifier"] != values["trailing_time"]
        ):
            return (
                normalized,
                None,
                [
                    Issue(
                        code="AMBIGUOUS_TIME",
                        detail="Multiple time modifiers need a supported combined construction",
                    )
                ],
            )
        time_modifier = values.get("time_modifier") or values.get("trailing_time")
        prep = values.get("preposition")
        if intent == "TRAIN_ARRIVAL" and prep == "from" and not development:
            return (
                normalized,
                None,
                [
                    Issue(
                        code="UNSUPPORTED_ROLE",
                        detail="Arrival from a platform is not an arrival destination",
                    )
                ],
            )
        for name in SLOT_TYPES:
            if values.get(name) is not None:
                a, b = match.span(
                    "implicit_train_identifier"
                    if name == "train_identifier" and values.get("implicit_train_identifier")
                    else name
                )
                start, end = offsets[a], offsets[b - 1] + 1
                raw_slots[name], spans[name] = text[start:end], (start, end)
        if development and values.get("place"):
            role = "source" if prep == "from" else "destination"
            if role in raw_slots:
                return (
                    normalized,
                    None,
                    [
                        Issue(
                            code="AMBIGUOUS_ROLE", detail="Multiple values for the same place role"
                        )
                    ],
                )
            a, b = match.span("place")
            start, end = offsets[a], offsets[b - 1] + 1
            raw_slots[role], spans[role] = text[start:end], (start, end)
            relations[role] = {"from": "event_origin", "to": "event_destination"}.get(
                prep, "event_location"
            )
        if "platform_identifier" in raw_slots:
            relations["platform_identifier"] = (
                "origin" if prep == "from" else "destination" if prep == "to" else "location"
            )
        for role in ("source", "destination"):
            if role in raw_slots:
                relations.setdefault(
                    role, "route_origin" if role == "source" else "route_destination"
                )
    valid_signature = validate_signature(intent, temporal, raw_slots)
    if development:
        valid_signature = DOMAIN_INTENTS.get(temporal) == intent and set(raw_slots) <= set(
            SLOT_TYPES
        )
    if not valid_signature:
        return (
            normalized,
            None,
            [
                Issue(
                    code="INVALID_MEANING",
                    detail="Intent, temporal state and required slot roles are inconsistent",
                )
            ],
        )
    slots = {}
    for name, raw in raw_slots.items():
        slots[name], issue = _slot(name, raw, station, service_date, *spans.get(name, (None, None)))
        if issue:
            issues.append(issue)
    if (
        intent == "PLATFORM_CHANGE"
        and "old_platform" in slots
        and "new_platform" in slots
        and slots["old_platform"].value == slots["new_platform"].value
    ):
        issues.append(
            Issue(code="UNCHANGED_PLATFORM", detail="Old and new platform identifiers must differ")
        )
    meaning = (DevelopmentMeaning if development else Meaning)(
        station_id=station_id,
        intent=intent,
        temporal_state=temporal,
        polarity=polarity,
        priority="P1" if intent in {"TRAIN_CANCELLATION", "PLATFORM_CHANGE"} else "P2",
        slots=slots,
        **(
            {"relations": relations, "event_form": event_form, "time_modifier": time_modifier}
            if development
            else {}
        ),
    )
    return normalized, meaning, issues


def structured_caption(fields: StructuredMeaning):
    """Display exactly the entered structure; never accept an unrelated caller caption."""
    status = fields.temporal_state.replace("_", " ").lower()
    prefix = "NOT " if fields.polarity == "NEGATIVE" else ""
    return f"{prefix}{status}: " + "; ".join(
        f"{key.replace('_', ' ')} {value}" for key, value in fields.slots.items()
    )
