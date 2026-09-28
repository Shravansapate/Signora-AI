"""Semantic and draft construction assertions, not ISL linguistic validation."""

import pytest
from pydantic import ValidationError

from app.announcement_schema import DevelopmentGlossPolicy, StationDefinition, StructuredMeaning
from app.gloss import construct_gloss
from app.meaning import parse_meaning

STATION = StationDefinition(name="Nagpur", platforms=[str(i) for i in range(1, 9)])


@pytest.mark.parametrize(
    "source,temporal,gloss",
    [
        ("Train 1201 arrives at platform 2", "ARRIVAL_UNSPECIFIED", "TRAIN|1201|PLATFORM|2|ARRIVE"),
        (
            "Train 1201 is arriving at platform 2",
            "ARRIVING_NOW",
            "TRAIN|1201|PLATFORM|2|NOW RIGHT NOW|ARRIVE",
        ),
        ("Train 1201 arrives on platform 2", "ARRIVAL_UNSPECIFIED", "TRAIN|1201|PLATFORM|2|ARRIVE"),
        (
            "Train 2245 departs from platform 3",
            "DEPARTURE_UNSPECIFIED",
            "TRAIN|2245|PLATFORM|3|DEPART",
        ),
        (
            "Train 1201 will arrive at platform 5",
            "SCHEDULED_ARRIVAL",
            "TRAIN|1201|PLATFORM|5|FUTURE|ARRIVE",
        ),
        (
            "Train 1201 is arriving on platform number 2",
            "ARRIVING_NOW",
            "TRAIN|1201|PLATFORM|2|NOW RIGHT NOW|ARRIVE",
        ),
        ("The train arrives at platform 2", "ARRIVAL_UNSPECIFIED", "TRAIN|PLATFORM|2|ARRIVE"),
        ("Train 1201 has been delayed", "DELAYED", "TRAIN|1201|DELAY"),
        ("Train 1201 will arrive shortly", "SCHEDULED_ARRIVAL", "TRAIN|1201|FUTURE|SHORTLY|ARRIVE"),
        (
            "Train 00120 has arrived at platform 2",
            "ALREADY_ARRIVED",
            "TRAIN|00120|PLATFORM|2|ALREADY|ARRIVE",
        ),
        (
            "Train 1201 will not arrive at platform 2",
            "SCHEDULED_ARRIVAL",
            "TRAIN|1201|PLATFORM|2|FUTURE|NOT|ARRIVE",
        ),
    ],
)
def test_domain_variations_preserve_semantics(source, temporal, gloss):
    normalized, meaning, issues = parse_meaning(source, "NAGPUR", STATION, development=True)
    assert normalized == source.lower()
    assert meaning is not None and not issues
    assert meaning.temporal_state == temporal
    assert [
        unit.token for unit in construct_gloss(meaning, STATION.development_gloss)
    ] == gloss.split("|")
    assert meaning.polarity == ("NEGATIVE" if " not " in source else "POSITIVE")


def test_proper_name_is_typed_and_original_spans_survive():
    source = "Train 1201 arrives at Chhatrapati Shivaji Maharaj Terminus"
    _, meaning, issues = parse_meaning(source, "NAGPUR", STATION, development=True)
    name = meaning.slots["destination"]
    assert name.raw == "Chhatrapati Shivaji Maharaj Terminus"
    assert source[name.source_start : name.source_end] == name.raw
    assert meaning.relations["destination"] == "event_location"
    units = construct_gloss(meaning, STATION.development_gloss)
    assert [u.token for u in units] == ["TRAIN", "1201", "STATION", name.raw.upper(), "ARRIVE"]
    assert units[3].kind == "NAME"
    assert issues[0].slot == "destination"


def test_relationships_are_preserved_not_deleted_as_stopwords():
    for prep, relation in (("from", "origin"), ("to", "destination"), ("at", "location")):
        _, meaning, _ = parse_meaning(
            f"Train 1201 arrives {prep} platform 2", "NAGPUR", STATION, development=True
        )
        assert meaning.relations["platform_identifier"] == relation
        gloss = [unit.token for unit in construct_gloss(meaning, STATION.development_gloss)]
        assert ("FROM" in gloss) == (prep == "from")
    for tail in ("before noon", "after departure", "between platform 2 and platform 3"):
        _, meaning, issues = parse_meaning(
            f"Train 1201 arrives {tail}", "NAGPUR", STATION, development=True
        )
        assert meaning is None and issues[0].code == "UNSUPPORTED_GRAMMAR"


def test_configurable_order_cannot_silently_drop_semantic_groups():
    with pytest.raises(ValidationError):
        DevelopmentGlossPolicy(order=["event"])
    policy = STATION.development_gloss.model_copy(
        update={"order": list(reversed(STATION.development_gloss.order))}
    )
    _, meaning, _ = parse_meaning(
        "Train 1201 arrives at platform 2", "NAGPUR", STATION, development=True
    )
    assert construct_gloss(meaning, policy)[0].token == "ARRIVE"


def test_strict_parser_is_not_relaxed():
    _, meaning, issues = parse_meaning("Train 1201 arrives at platform 2", "NAGPUR", STATION)
    assert meaning is None and issues


def test_two_time_modifiers_are_not_silently_collapsed():
    _, meaning, issues = parse_meaning(
        "Train 1201 will arrive shortly at platform 2 tomorrow",
        "NAGPUR",
        STATION,
        development=True,
    )
    assert meaning is None and issues[0].code == "AMBIGUOUS_TIME"


def test_structured_invalid_duration_and_partial_platform_change_do_not_crash():
    fields = StructuredMeaning(
        intent="TRAIN_DELAY",
        temporal_state="DELAYED",
        slots={"train_identifier": "1201", "delay_duration": "unknown"},
    )
    _, meaning, issues = parse_meaning("", "NAGPUR", STATION, structured=fields, development=True)
    assert issues and construct_gloss(meaning, STATION.development_gloss)[-1].kind == "DURATION"
    fields = StructuredMeaning(
        intent="PLATFORM_CHANGE", temporal_state="CURRENT_CHANGE", slots={"new_platform": "2"}
    )
    _, meaning, _ = parse_meaning("", "NAGPUR", STATION, structured=fields, development=True)
    assert [u.token for u in construct_gloss(meaning, STATION.development_gloss)] == [
        "TRAIN",
        "TO",
        "PLATFORM",
        "2",
        "CHANGE",
    ]


def test_clock_and_platform_suffix_remain_distinct_from_quantities():
    fields = StructuredMeaning(
        intent="TRAIN_ARRIVAL",
        temporal_state="SCHEDULED_ARRIVAL",
        slots={"train_identifier": "00120", "platform_identifier": "2A", "clock_time": "12:30 PM"},
    )
    _, meaning, _ = parse_meaning("", "NAGPUR", STATION, structured=fields, development=True)
    units = construct_gloss(meaning, STATION.development_gloss)
    assert any(u.token == "2A" and u.kind == "PLATFORM_IDENTIFIER" for u in units)
    assert any(u.token == "12:30 PM" and u.kind == "CLOCK_TIME" for u in units)
